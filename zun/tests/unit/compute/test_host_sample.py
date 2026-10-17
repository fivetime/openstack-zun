#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
#    implied. See the License for the specific language governing
#    permissions and limitations under the License.

"""One host's containers and capsules, read when a collector asks.

A collector polls hosts, not containers: one call per host however many
it runs. Each object comes with what is needed to account for it without
counting anything twice -- a capsule's allocation is worked out the way
k8s does and is not the sum of its containers, so a capsule's container
names its capsule and says its allocation is already counted there.
"""

from unittest import mock

from zun.compute import manager
from zun.container import driver as driver_module
from zun.tests import base


def _obj(uuid, labels=None, memory='512', **kw):
    obj = mock.Mock(uuid=uuid, labels=labels or {}, memory=memory, cpu=1.0,
                    disk=10, project_id='p-1', user_id='u-1', image='img',
                    runtime='runc', status='Running', started_at=None)
    obj.name = kw.pop('name', uuid)
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


class HostSampleTest(base.TestCase):

    def setUp(self):
        super(HostSampleTest, self).setUp()
        self.mgr = manager.Manager.__new__(manager.Manager)
        self.mgr.host = 'node-1'
        self.mgr.driver = mock.Mock(spec=driver_module.ContainerDriver)
        self.mgr.capsule_driver = mock.Mock()
        self.plain = _obj('c-1', labels={'metering.autoscale': 'yes',
                                         'private': 'x'})
        self.child = _obj('cc-1')
        self.init = _obj('ci-1')
        self.capsule = _obj('cap-1', containers=[self.child],
                            init_containers=[self.init])
        p1 = mock.patch('zun.objects.Container.list_by_host',
                        return_value=[self.plain])
        p2 = mock.patch('zun.objects.Capsule.list_by_host',
                        return_value=[self.capsule])
        p3 = mock.patch.object(manager.time, 'sleep')
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)
        self.mgr.driver.sample_counters.return_value = {
            'c-1': {'timestamp': 't', 'cpu': {'total_ns': 1}}}
        self.mgr.capsule_driver.sample_capsules.return_value = {
            'cap-1': {'timestamp': 't', 'cpu': {'total_ns': 9}}}
        self.mgr.capsule_driver.sample_counters.return_value = {
            'cc-1': {'timestamp': 't', 'cpu': {'total_ns': 3}},
            'ci-1': {'timestamp': 't', 'cpu': {'total_ns': 4}}}

    def _by_uuid(self, answer):
        return {e['uuid']: e for e in answer['results']}

    def test_containers_and_capsules_by_default_children_not_read(self):
        """Capsule containers off unless asked: the common bill is a pod."""
        found = self._by_uuid(self.mgr.host_sample({}))

        self.assertEqual({'c-1', 'cap-1'}, set(found))
        self.mgr.capsule_driver.sample_counters.assert_not_called()

    def test_a_capsule_is_read_as_a_whole_with_its_children_named(self):
        found = self._by_uuid(self.mgr.host_sample({}, types=['capsule']))

        cap = found['cap-1']
        self.assertEqual('capsule', cap['container_type'])
        self.assertEqual(9, cap['reading']['cpu']['total_ns'])
        self.assertEqual(['cc-1', 'ci-1'], cap['children'])
        self.assertIn('max(', cap['allocation_basis'])
        self.mgr.driver.sample_counters.assert_not_called()

    def test_a_capsule_container_points_at_its_capsule(self):
        """So a reader can leave it out rather than count it twice."""
        found = self._by_uuid(
            self.mgr.host_sample({}, types=['capsule_container']))

        self.assertEqual({'cc-1', 'ci-1'}, set(found))
        for uuid, kind in (('cc-1', 'capsule_container'),
                           ('ci-1', 'capsule_init_container')):
            self.assertEqual(kind, found[uuid]['container_type'])
            self.assertEqual('cap-1', found[uuid]['capsule_uuid'])
            self.assertTrue(found[uuid]['allocation_included_in_parent'])
        self.mgr.capsule_driver.sample_capsules.assert_not_called()

    def test_owner_and_allocation_travel_with_the_reading(self):
        entry = self._by_uuid(self.mgr.host_sample(
            {}, types=['container']))['c-1']

        self.assertEqual('p-1', entry['project_id'])
        self.assertEqual('node-1', entry['host'])
        self.assertEqual({'cpu': 1.0, 'memory_mb': 512, 'disk_gb': 10},
                         entry['allocation'])
        self.assertIsNone(entry['previous'])

    def test_only_metering_labels_leave_the_node(self):
        entry = self._by_uuid(self.mgr.host_sample(
            {}, types=['container']))['c-1']

        self.assertEqual({'metering.autoscale': 'yes'}, entry['labels'])

    def test_a_label_narrows_what_is_read(self):
        found = self._by_uuid(self.mgr.host_sample(
            {}, label='metering.autoscale=yes'))

        self.assertEqual({'c-1'}, set(found))
        self.mgr.capsule_driver.sample_capsules.assert_not_called()

    def test_an_object_without_a_reading_is_named_not_zeroed(self):
        self.mgr.capsule_driver.sample_capsules.return_value = {}

        answer = self.mgr.host_sample({})

        self.assertEqual([{'uuid': 'cap-1', 'container_type': 'capsule',
                           'reason': 'the runtime gave no reading'}],
                         answer['unavailable'])
        self.assertNotIn('cap-1', self._by_uuid(answer))

    def test_an_interval_reads_twice_and_returns_both(self):
        self.mgr.driver.sample_counters.side_effect = [
            {'c-1': {'cpu': {'total_ns': 1}}},
            {'c-1': {'cpu': {'total_ns': 2}}}]

        entry = self._by_uuid(self.mgr.host_sample(
            {}, types=['container'], interval=1))['c-1']

        manager.time.sleep.assert_called_once_with(1)
        self.assertEqual(2, entry['reading']['cpu']['total_ns'])
        self.assertEqual(1, entry['previous']['cpu']['total_ns'])

    def test_memory_is_whole_mib_whatever_the_record_wrote(self):
        for text, mib in (('512', 512), ('512M', 512), ('1G', 1024),
                          ('2048m', 2048), (None, None), ('', None)):
            self.assertEqual(mib, manager.Manager._memory_mb(text), text)
