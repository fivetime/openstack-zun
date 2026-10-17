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

"""Readings and sizes are taken when somebody asks, never on a schedule.

zun used to sample every container every fifteen seconds and walk every
writable layer every hour, whether or not anything was listening; a
deployment without metering paid for it all the same. Now a node reads
only what a request names, and a request that spans nodes asks them all
at once, so one node that does not answer costs only its own figures.
"""

from unittest import mock

from zun.compute import api as compute_api
from zun.compute import manager
from zun.container import driver
from zun.tests import base


class ReadingTimeTest(base.TestCase):
    """Every driver's reading time comes out in one form."""

    def test_docker_rfc3339_with_nanoseconds(self):
        self.assertEqual('2026-10-12T10:40:01.123456+00:00',
                         driver.reading_time('2026-10-12T10:40:01.123456789Z'))

    def test_cri_nanoseconds_since_the_epoch(self):
        self.assertEqual('2026-10-16T21:30:33.123456+00:00',
                         driver.reading_time(1792186233123456789))

    def test_an_offset_is_folded_into_utc(self):
        self.assertEqual('2026-10-12T10:40:01.500000+00:00',
                         driver.reading_time('2026-10-12T19:40:01.5+09:00'))

    def test_no_fraction(self):
        self.assertEqual('2026-10-12T10:40:01+00:00',
                         driver.reading_time('2026-10-12T10:40:01Z'))

    def test_each_runtimes_no_reading_is_none(self):
        for stamp in (None, 0, -1, '', '0001-01-01T00:00:00Z', 'garbage',
                      True):
            self.assertIsNone(driver.reading_time(stamp), stamp)


def _manager():
    mgr = manager.Manager.__new__(manager.Manager)
    mgr.driver = mock.Mock()
    return mgr


class ContainerSampleTest(base.TestCase):

    def setUp(self):
        super(ContainerSampleTest, self).setUp()
        self.mgr = _manager()
        self.containers = [mock.Mock(uuid='u-1'), mock.Mock(uuid='u-2')]
        p = mock.patch.object(manager.time, 'sleep')
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def test_one_reading_without_an_interval(self):
        self.mgr.driver.sample_counters.return_value = {'u-1': {'n': 1}}

        found = self.mgr.container_sample({}, self.containers)

        self.assertEqual({'u-1': {'current': {'n': 1}, 'previous': None}},
                         found)
        self.assertEqual(1, self.mgr.driver.sample_counters.call_count)
        self.sleep.assert_not_called()

    def test_an_interval_reads_everything_twice_and_waits_once(self):
        """docker's own two-sample stats cost a second per container."""
        self.mgr.driver.sample_counters.side_effect = [
            {'u-1': {'n': 1}, 'u-2': {'n': 10}},
            {'u-1': {'n': 2}, 'u-2': {'n': 20}},
        ]

        found = self.mgr.container_sample({}, self.containers, interval=1)

        self.sleep.assert_called_once_with(1)
        self.assertEqual({'current': {'n': 2}, 'previous': {'n': 1}},
                         found['u-1'])
        self.assertEqual({'current': {'n': 20}, 'previous': {'n': 10}},
                         found['u-2'])

    def test_the_interval_is_kept_short(self):
        """The RPC worker answering waits the whole interval."""
        self.mgr.driver.sample_counters.return_value = {}

        self.mgr.container_sample({}, self.containers, interval=3600)

        self.sleep.assert_called_once_with(manager.Manager.MAX_SAMPLE_INTERVAL)

    def test_a_container_the_runtime_did_not_answer_for_is_absent(self):
        self.mgr.driver.sample_counters.side_effect = [
            {'u-1': {'n': 1}}, {'u-1': {'n': 2}}]

        found = self.mgr.container_sample({}, self.containers, interval=1)

        self.assertEqual({'u-1'}, set(found))

    def test_nothing_asked_reads_nothing(self):
        self.assertEqual({}, self.mgr.container_sample({}, []))
        self.mgr.driver.sample_counters.assert_not_called()


class ContainerSizesTest(base.TestCase):

    def test_measured_now_with_the_time(self):
        mgr = _manager()
        mgr.driver.measure_writable_layers.return_value = {'u-1': 4096}

        found = mgr.container_sizes({}, [mock.Mock(uuid='u-1')])

        self.assertEqual(4096, found['u-1']['size_rw'])
        self.assertTrue(found['u-1']['measured_at'].endswith('+00:00'))

    def test_nothing_asked_measures_nothing(self):
        mgr = _manager()

        self.assertEqual({}, mgr.container_sizes({}, []))
        mgr.driver.measure_writable_layers.assert_not_called()


class FanOutTest(base.TestCase):
    """Every node concerned is asked at once; a failing one is named."""

    def setUp(self):
        super(FanOutTest, self).setUp()
        self.api = compute_api.API.__new__(compute_api.API)
        self.api.rpcapi = mock.Mock()
        self.on_a = [mock.Mock(uuid='a-1', host='a')]
        self.on_b = [mock.Mock(uuid='b-1', host='b'),
                     mock.Mock(uuid='b-2', host='b')]

    def test_one_call_per_host_with_its_own_containers(self):
        self.api.rpcapi.container_sample.side_effect = (
            lambda ctx, host, members, interval: {
                c.uuid: {'current': {}, 'previous': None} for c in members})

        found, failed = self.api.container_sample(
            {}, self.on_a + self.on_b)

        self.assertEqual({'a-1', 'b-1', 'b-2'}, set(found))
        self.assertEqual({}, failed)
        hosts = sorted(c.args[1] for c in
                       self.api.rpcapi.container_sample.call_args_list)
        self.assertEqual(['a', 'b'], hosts)

    def test_a_host_that_fails_costs_only_its_own_figures(self):
        def answer(ctx, host, members, interval):
            if host == 'b':
                raise RuntimeError('timed out')
            return {c.uuid: {'current': {}, 'previous': None}
                    for c in members}
        self.api.rpcapi.container_sample.side_effect = answer

        found, failed = self.api.container_sample(
            {}, self.on_a + self.on_b)

        self.assertEqual({'a-1'}, set(found))
        self.assertEqual({'b': 'RuntimeError'}, failed)

    def test_a_container_with_no_host_is_not_asked_about(self):
        found, failed = self.api.container_sizes(
            {}, [mock.Mock(uuid='x', host=None)])

        self.assertEqual(({}, {}), (found, failed))
        self.api.rpcapi.container_sizes.assert_not_called()
