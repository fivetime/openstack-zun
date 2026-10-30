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

"""A capsule's runtime is judged by what the capsule driver offers.

On a docker host the container driver is dockerd and the capsule driver is
containerd's CRI; their runtime lists are configured apart and need not
match. Measured on zun-worker4: kata-dragonball was a CRI handler, not yet
a dockerd runtime, and a capsule naming it was NoValidHost; gVisor was runsc
to dockerd and gvisor to the CRI (gVisor's own docs name both runsc).
"""

from unittest import mock

import grpc

from zun.common import context
from zun.compute import compute_node_tracker
from zun.container.cri import driver as cri_driver
from zun import objects
from zun.scheduler.filters import runtime_filter
from zun.scheduler import host_state
from zun.tests import base
from zun.tests.unit.container import fake_driver
from zun.tests.unit.scheduler import fakes


DOCKERD = ['crun', 'kata-clh', 'kata-qemu', 'runsc']
CRI = ['crun', 'gvisor', 'kata-clh', 'kata-dragonball', 'kata-qemu', 'runc']


class TestRuntimeFilterByRequestType(base.TestCase):

    def setUp(self):
        super(TestRuntimeFilterByRequestType, self).setUp()
        self.context = context.RequestContext('fake_user', 'fake_project')
        self.filt = runtime_filter.RuntimeFilter()
        self.host = fakes.FakeHostState('testhost')
        self.host.runtimes = DOCKERD
        self.host.capsule_runtimes = CRI

    def _capsule(self, runtime):
        capsule = objects.Capsule(self.context)
        capsule.runtime = runtime
        return capsule

    def _container(self, runtime):
        container = objects.Container(self.context)
        container.runtime = runtime
        return container

    def test_capsule_passes_on_a_handler_dockerd_lacks(self):
        self.assertTrue(self.filt.host_passes(
            self.host, self._capsule('kata-dragonball'), {}))

    def test_capsule_fails_on_a_dockerd_only_name(self):
        # A host whose dockerd names gVisor runsc and whose CRI names it
        # gvisor.
        self.assertFalse(self.filt.host_passes(
            self.host, self._capsule('runsc'), {}))
        self.assertTrue(self.filt.host_passes(
            self.host, self._capsule('gvisor'), {}))

    def test_container_is_still_judged_by_dockerd(self):
        self.assertFalse(self.filt.host_passes(
            self.host, self._container('kata-dragonball'), {}))
        self.assertTrue(self.filt.host_passes(
            self.host, self._container('runsc'), {}))

    def test_capsule_on_a_host_that_has_not_reported_falls_back(self):
        # A zun-compute from before the split leaves capsule_runtimes null;
        # judged as every host was before, not as offering nothing.
        self.host.capsule_runtimes = None
        self.assertTrue(self.filt.host_passes(
            self.host, self._capsule('runsc'), {}))
        self.assertFalse(self.filt.host_passes(
            self.host, self._capsule('kata-dragonball'), {}))

    def test_capsule_on_a_host_that_reports_none_offered(self):
        # Known empty is not unknown: nothing a capsule names passes.
        self.host.capsule_runtimes = []
        self.assertFalse(self.filt.host_passes(
            self.host, self._capsule('crun'), {}))

    def test_capsule_without_a_runtime_passes(self):
        self.host.capsule_runtimes = []
        self.assertTrue(self.filt.host_passes(
            self.host, self._capsule(None), {}))

    def test_host_state_carries_capsule_runtimes(self):
        state = host_state.HostState('testhost')
        self.assertIsNone(state.capsule_runtimes)
        node = objects.ComputeNode(self.context)
        for field, value in (
                ('rp_uuid', 'f6e5bd4c-3c6c-4b8b-9c6c-1a9a7d0b3c11'),
                ('mem_available', 1), ('mem_total', 1), ('mem_free', 1),
                ('mem_used', 0), ('cpus', 1), ('cpu_used', 0),
                ('disk_total', 1), ('disk_used', 0), ('numa_topology', None),
                ('labels', None), ('pci_device_pools', None),
                ('disk_quota_supported', False), ('runtimes', DOCKERD),
                ('capsule_runtimes', CRI), ('enable_cpu_pinning', False),
                ('updated_at', None)):
            setattr(node, field, value)
        state.update(compute_node=node)
        self.assertEqual(DOCKERD, state.runtimes)
        self.assertEqual(CRI, state.capsule_runtimes)


class TestTrackerRecordsCapsuleRuntimes(base.TestCase):

    def setUp(self):
        super(TestTrackerRecordsCapsuleRuntimes, self).setUp()
        # The real SchedulerClient builds a placement client from keystone
        # options that only some earlier test in the run has set.
        patcher = mock.patch.object(
            compute_node_tracker.scheduler_client, 'SchedulerClient')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.container_driver = fake_driver.FakeDriver()
        self.capsule_driver = fake_driver.FakeDriver()
        self.tracker = compute_node_tracker.ComputeNodeTracker(
            'testhost', self.container_driver, self.capsule_driver,
            mock.MagicMock())
        self.node = objects.ComputeNode(self.context)
        self.node.runtimes = ['old']
        self.node.capsule_runtimes = ['old']
        for name, value in (
                ('_get_compute_node', self.node),
                ('_get_node_rp_uuid', 'rp'),
                ('_setup_pci_tracker', None),
                ('_update_available_resource', None)):
            patcher = mock.patch.object(
                compute_node_tracker.ComputeNodeTracker, name,
                return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        resources = mock.patch.object(
            self.container_driver, 'get_available_resources',
            return_value={'runtimes': DOCKERD})
        resources.start()
        self.addCleanup(resources.stop)

    def test_records_the_capsule_drivers_list_not_the_container_drivers(self):
        with mock.patch.object(self.capsule_driver, 'capsule_runtimes',
                               return_value=CRI):
            self.tracker.update_available_resources(self.context)
        self.assertEqual(DOCKERD, self.node.runtimes)
        self.assertEqual(CRI, self.node.capsule_runtimes)

    def test_unknown_keeps_the_last_record(self):
        with mock.patch.object(self.capsule_driver, 'capsule_runtimes',
                               return_value=None):
            self.tracker.update_available_resources(self.context)
        self.assertEqual(['old'], self.node.capsule_runtimes)

    def test_a_failure_to_ask_keeps_the_record_and_the_update(self):
        with mock.patch.object(self.capsule_driver, 'capsule_runtimes',
                               side_effect=RuntimeError('down')):
            self.tracker.update_available_resources(self.context)
        self.assertEqual(['old'], self.node.capsule_runtimes)
        self.assertEqual(DOCKERD, self.node.runtimes)

    def test_a_driver_that_cannot_tell_reports_none(self):
        self.assertIsNone(fake_driver.FakeDriver().capsule_runtimes())


class _Cri(cri_driver.CriDriver):
    """A CRI driver with the gRPC channel left out."""

    def __init__(self):
        self.runtime_stub = mock.Mock()


def _handler(name):
    handler = mock.Mock()
    handler.name = name
    return handler


class TestCriCapsuleRuntimes(base.TestCase):

    def test_lists_containerds_handlers(self):
        drv = _Cri()
        drv.runtime_stub.Status.return_value = mock.Mock(
            runtime_handlers=[_handler('kata-dragonball'), _handler('runc'),
                              _handler('')])
        self.assertEqual(['kata-dragonball', 'runc'],
                         drv.capsule_runtimes())

    def test_unanswered_status_is_unknown_not_empty(self):
        drv = _Cri()
        drv.runtime_stub.Status.side_effect = grpc.RpcError()
        self.assertIsNone(drv.capsule_runtimes())
        # The container driver's own list keeps reporting empty, as before.
        self.assertEqual([], drv._available_runtimes())
