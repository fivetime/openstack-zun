#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from unittest import mock

import testtools

from zun.compute import notifications
from zun.tests import base


class FakeContainer(object):
    uuid = '318d2e59-f15b-4992-ae41-51d5bfee3998'
    name = 'web'
    user_id = 'u-1'
    project_id = 'p-1'
    host = 'compute-1'
    image = 'alpine:latest'
    status = 'Running'
    task_state = None
    cpu = 1.0
    memory = 512
    disk = 10
    created_at = None
    started_at = None
    labels = {'io.daas.managed': 'true'}


class TestPayload(base.TestCase):

    def test_it_carries_what_a_bill_needs(self):
        payload = notifications._payload(FakeContainer())

        for field in ('uuid', 'project_id', 'cpu', 'memory', 'disk', 'host'):
            self.assertIn(field, payload)

    def test_tenant_id_is_present_for_older_consumers(self):
        """ceilometer's meters key on tenant_id, not project_id."""
        payload = notifications._payload(FakeContainer())

        self.assertEqual(payload['project_id'], payload['tenant_id'])


class TestSending(base.TestCase):

    @mock.patch.object(notifications.rpc, 'get_notifier')
    def test_a_phase_makes_the_event_type(self, get_notifier):
        notifications.notify(mock.Mock(), FakeContainer(), 'create', 'end')

        event_type = get_notifier.return_value.info.call_args[0][1]
        self.assertEqual('container.create.end', event_type)

    @mock.patch.object(notifications.rpc, 'get_notifier')
    def test_without_a_phase_it_is_a_bare_event(self, get_notifier):
        notifications.notify(mock.Mock(), FakeContainer(), 'rebuild')

        self.assertEqual('container.rebuild',
                         get_notifier.return_value.info.call_args[0][1])

    @mock.patch.object(notifications.rpc, 'get_notifier')
    def test_a_deployment_that_never_configured_one(self, get_notifier):
        """A container is still created where nothing is listening."""
        get_notifier.return_value = None

        notifications.notify(mock.Mock(), FakeContainer(), 'create', 'start')

    @mock.patch.object(notifications.rpc, 'get_notifier')
    def test_a_busy_bus_does_not_fail_the_operation(self, get_notifier):
        """The container was created; saying so is what went wrong."""
        get_notifier.return_value.info.side_effect = RuntimeError('bus down')

        notifications.notify(mock.Mock(), FakeContainer(), 'create', 'end')

    @mock.patch.object(notifications.rpc, 'get_notifier')
    def test_an_error_is_told_apart_from_a_missing_end(self, get_notifier):
        """A consumer cannot tell "failed" from "never attempted" otherwise."""
        notifications.notify_error(mock.Mock(), FakeContainer(), 'create',
                                   RuntimeError('no capacity'))

        called = get_notifier.return_value.error.call_args
        self.assertEqual('container.create.error', called[0][1])
        self.assertIn('no capacity', called[0][2]['reason'])


class TestLifecycle(base.TestCase):
    """start, end and error stay one whole, whichever way the block leaves."""

    def setUp(self):
        super(TestLifecycle, self).setUp()
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        self.mock_notifier = p.start()
        self.addCleanup(p.stop)

    def _events(self):
        return [c.args[1] for c in self.notifier.info.call_args_list]

    def test_a_clean_block_sends_start_then_end(self):
        with notifications.lifecycle({}, FakeContainer(), 'stop'):
            pass
        self.assertEqual(['container.stop.start', 'container.stop.end'],
                         self._events())

    def test_a_failing_block_sends_start_then_error_and_reraises(self):
        with testtools.ExpectedException(ValueError):
            with notifications.lifecycle({}, FakeContainer(), 'stop'):
                raise ValueError('boom')
        self.assertEqual(['container.stop.start'], self._events())
        errs = [c.args[1] for c in self.notifier.error.call_args_list]
        self.assertEqual(['container.stop.error'], errs)

    def test_no_end_is_sent_when_the_block_failed(self):
        try:
            with notifications.lifecycle({}, FakeContainer(), 'pause'):
                raise RuntimeError()
        except RuntimeError:
            pass
        self.assertNotIn('container.pause.end', self._events())


class TestStateChange(base.TestCase):
    """save() announces the state a container reached."""

    def setUp(self):
        super(TestStateChange, self).setUp()
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        p.start()
        self.addCleanup(p.stop)

    def _sent(self, changes):
        notifications.notify_state_change({}, FakeContainer(), changes)
        return self.notifier.info.call_args

    def test_a_status_move_is_announced_with_which_field_moved(self):
        call = self._sent({'status': 'Stopped'})
        self.assertEqual('container.update', call.args[1])
        self.assertEqual(['status'], call.args[2]['changed'])

    def test_a_resize_is_announced_too(self):
        """cpu and memory changes re-rate a bill."""
        call = self._sent({'memory': 1024, 'cpu': 2.0})
        self.assertEqual({'cpu', 'memory'}, set(call.args[2]['changed']))

    def test_a_change_of_nothing_billable_is_silent(self):
        notifications.notify_state_change({}, FakeContainer(),
                                          {'status_reason': 'x'})
        self.notifier.info.assert_not_called()


class TestUsageIsAlsoExists(base.TestCase):
    """One report a minute: what is used, and that it was there to use it."""

    def setUp(self):
        super(TestUsageIsAlsoExists, self).setUp()
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        p.start()
        self.addCleanup(p.stop)

    def test_it_carries_status_and_bytes_per_container(self):
        c = FakeContainer()
        notifications.notify_usage({}, 'compute-1', [c], {c.uuid: 4096})
        entry = self.notifier.info.call_args.args[2]['containers'][0]
        self.assertEqual(4096, entry['size_rw'])
        self.assertEqual('Running', entry['status'])

    def test_an_unmeasured_container_still_reports_that_it_exists(self):
        c = FakeContainer()
        notifications.notify_usage({}, 'compute-1', [c], {})
        entry = self.notifier.info.call_args.args[2]['containers'][0]
        self.assertIsNone(entry['size_rw'])
        self.assertEqual('Running', entry['status'])

    def test_the_window_is_carried_so_periods_abut(self):
        import datetime
        start = datetime.datetime(2026, 1, 1, 0, 0, 0)
        notifications.notify_usage({}, 'compute-1', [], {}, start)
        p = self.notifier.info.call_args.args[2]
        self.assertEqual(start.isoformat(), p['audit_period_beginning'])


class TestExists(base.TestCase):
    """The billing heartbeat: one message per container, per period.

    Its own message rather than a share of a batch, because a meter is
    made of a container: what a bill is rated from has to travel together
    for one of them.
    """

    def setUp(self):
        super(TestExists, self).setUp()
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        p.start()
        self.addCleanup(p.stop)

    def _sent(self, **kw):
        notifications.notify_exists({}, FakeContainer(), **kw)
        return self.notifier.info.call_args

    def test_it_is_its_own_event(self):
        self.assertEqual('container.exists', self._sent().args[1])

    def test_it_carries_who_owns_it_and_what_it_was_given(self):
        payload = self._sent().args[2]
        self.assertEqual('p-1', payload['project_id'])
        self.assertEqual('u-1', payload['user_id'])
        self.assertEqual(1.0, payload['cpu'])
        self.assertEqual(512, payload['memory'])

    def test_the_period_travels_with_the_container(self):
        import datetime
        start = datetime.datetime(2026, 1, 1, 0, 0, 0)
        payload = self._sent(window_start=start).args[2]
        self.assertEqual(start.isoformat(), payload['audit_period_beginning'])
        self.assertTrue(payload['audit_period_ending'])

    def test_an_unmeasured_container_is_still_billable(self):
        """What it cost to exist does not depend on measuring it."""
        payload = self._sent(size_rw=None).args[2]
        self.assertIsNone(payload['size_rw'])
        self.assertEqual('p-1', payload['project_id'])


class TestReportingInterval(base.TestCase):
    """The interval has to be read when the task runs, not when it is defined.

    A decorator's arguments are evaluated when the class is defined, which
    is before the config file has been read: `spacing=CONF...` freezes
    whatever the default happened to be, and an operator's setting never
    applies. Measured on a live deployment -- exists_interval was set to
    300 and the task kept its compiled-in 3600.
    """

    def setUp(self):
        super(TestReportingInterval, self).setUp()
        from zun.compute import manager
        self.manager = manager.Manager.__new__(manager.Manager)
        self.manager._last_report = {}

    def test_the_first_ask_is_always_due(self):
        self.assertTrue(self.manager._due('exists', 3600))

    def test_a_second_ask_inside_the_interval_is_not(self):
        self.manager._due('exists', 3600)
        self.assertFalse(self.manager._due('exists', 3600))

    def test_a_shorter_interval_takes_effect_at_once(self):
        """The point of the fix: the number is read now, not at import."""
        self.manager._due('exists', 3600)
        self.assertTrue(self.manager._due('exists', 0))

    def test_the_two_reports_keep_their_own_clocks(self):
        self.manager._due('exists', 3600)
        self.assertTrue(self.manager._due('usage', 3600))


class TestRebuiltNotification(base.TestCase):
    """A changed container id is the fact itself.

    The record names one container id. A runtime that rebuilt an exited
    container leaves a different one there, so the manager can see it
    happened without any driver having to report it -- which keeps
    notifications out of the drivers, where they do not belong.
    """

    def setUp(self):
        super(TestRebuiltNotification, self).setUp()
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        p.start()
        self.addCleanup(p.stop)

    def test_the_event_carries_the_id_that_was_replaced(self):
        notifications.notify({}, FakeContainer(), 'rebuilt',
                             extra={'previous_container_id': 'old-id',
                                    'reason': 'because'})

        call = self.notifier.info.call_args
        self.assertEqual('container.rebuilt', call.args[1])
        self.assertEqual('old-id', call.args[2]['previous_container_id'])
        self.assertEqual('because', call.args[2]['reason'])


def _reading(cpu_ns=14779014546000, usage=1742868480, cache=1343578112,
             limit=2147483648, read='2026-10-12T10:40:01.123456789Z'):
    """A sample_counters entry, as a kata-qemu container on docker gives it."""
    return {'timestamp': read,
            'cpu': {'total_ns': cpu_ns, 'system_ns': 1, 'online_cpus': 16},
            'memory': {'usage': usage, 'limit': limit, 'cache': cache}}


class TestMetrics(base.TestCase):
    """container.metrics: one running container's reading, for alarms."""

    def setUp(self):
        super().setUp()
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        p.start()
        self.addCleanup(p.stop)

    def _sent(self, sample):
        sent = notifications.notify_metrics({}, FakeContainer(), sample)
        return sent, self.notifier.info.call_args

    def test_one_message_per_container_with_the_reading(self):
        sent, call = self._sent(_reading())
        self.assertTrue(sent)
        self.assertEqual('container.metrics', call.args[1])
        payload = call.args[2]
        self.assertEqual(FakeContainer.uuid, payload['uuid'])
        self.assertEqual('p-1', payload['project_id'])
        self.assertEqual('u-1', payload['user_id'])
        self.assertEqual('compute-1', payload['host'])
        self.assertEqual(14779014546000, payload['cpu_time_ns'])
        # working set: usage less the inactive page cache
        self.assertEqual(1742868480 - 1343578112,
                         payload['memory_usage_bytes'])
        self.assertEqual(2147483648, payload['memory_limit_bytes'])

    def test_labels_travel_as_they_are(self):
        labels = {'metering.server_group': 'web-cluster',
                  'cluster_id': 'c-1', 'cluster_node_index': '3'}
        container = FakeContainer()
        container.labels = labels
        notifications.notify_metrics({}, container, _reading())
        payload = self.notifier.info.call_args.args[2]
        self.assertEqual(labels, payload['labels'])

    def test_the_reading_time_is_iso8601_utc(self):
        _, call = self._sent(_reading())
        self.assertEqual('2026-10-12T10:40:01.123456+00:00',
                         call.args[2]['timestamp'])

    def test_no_reading_time_falls_back_to_now(self):
        for read in (None, '0001-01-01T00:00:00Z', 'garbage'):
            _, call = self._sent(_reading(read=read))
            self.assertTrue(call.args[2]['timestamp'].endswith('+00:00'))

    def test_a_cri_reading_time_is_iso8601_utc(self):
        """The CRI gives nanoseconds since the epoch, not a string."""
        _, call = self._sent(_reading(read=1792186233123456789))
        self.assertEqual('2026-10-16T21:30:33.123456+00:00',
                         call.args[2]['timestamp'])

    def test_no_cri_reading_time_falls_back_to_now(self):
        _, call = self._sent(_reading(read=0))
        self.assertTrue(call.args[2]['timestamp'].endswith('+00:00'))

    def test_what_the_cri_driver_samples_is_reported(self):
        """The CRI driver's own reading, from the runtime's own message.

        Each side was tested with a reading shaped as it expected, and
        the two expected different shapes: every container.metrics round
        on a CRI node raised, and the node reported nothing.
        """
        from zun.container.cri import driver as cri_driver
        from zun.criapi import api_pb2
        driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        driver.runtime_stub = mock.Mock()
        driver.runtime_stub.ListContainerStats.return_value = (
            api_pb2.ListContainerStatsResponse(stats=[api_pb2.ContainerStats(
                attributes=api_pb2.ContainerAttributes(id='c-1'),
                cpu=api_pb2.CpuUsage(
                    timestamp=1792186233123456789,
                    usage_core_nano_seconds=api_pb2.UInt64Value(value=5000)),
                memory=api_pb2.MemoryUsage(
                    working_set_bytes=api_pb2.UInt64Value(value=4096)))]))
        container = FakeContainer()
        container.container_id = 'c-1'
        with mock.patch.object(driver, '_sandbox_networks', return_value={}):
            sample = driver.sample_counters({}, [container])[container.uuid]

        sent, call = self._sent(sample)

        self.assertTrue(sent)
        payload = call.args[2]
        self.assertEqual(5000, payload['cpu_time_ns'])
        self.assertEqual(4096, payload['memory_usage_bytes'])
        self.assertIsNone(payload['memory_limit_bytes'])
        self.assertEqual('2026-10-16T21:30:33.123456+00:00',
                         payload['timestamp'])

    def test_no_limit_is_null(self):
        _, call = self._sent(_reading(limit=0))
        self.assertIsNone(call.args[2]['memory_limit_bytes'])

    def test_a_container_without_a_reading_is_skipped(self):
        """Not reported as using nothing: a zero is a reading."""
        for sample in (None, {}, _reading(cpu_ns=None),
                       _reading(usage=None)):
            self.notifier.reset_mock()
            sent, _ = self._sent(sample)
            self.assertFalse(sent)
            self.notifier.info.assert_not_called()

    def test_a_busy_bus_is_not_an_error(self):
        self.notifier.info.side_effect = RuntimeError('bus down')
        sent, _ = self._sent(_reading())
        self.assertFalse(sent)


class TestMetricsRound(base.TestCase):
    """report_metrics: every running container, on a fixed grid."""

    def setUp(self):
        super().setUp()
        from zun.compute import manager
        from zun.container import driver as driver_module
        self.manager_mod = manager
        self.manager = manager.Manager.__new__(manager.Manager)
        self.manager._metrics_due = None
        self.manager.host = 'compute-1'
        self.manager.driver = mock.Mock(spec=driver_module.ContainerDriver)
        self.clock = [1000.0]
        p = mock.patch.object(manager.time, 'monotonic',
                              side_effect=lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)

    def _due(self, at, interval=60):
        self.clock[0] = at
        return self.manager._metrics_round_due(interval)

    def test_rounds_keep_to_the_grid(self):
        # Ticks every 5 s, starting off the grid: rounds land at the first
        # tick at or after 1000, 1060, 1120, ... -- never drifting later.
        sent_at = [t + 2 for t in range(1000, 1300, 5) if self._due(t + 2)]
        self.assertEqual([1002, 1062, 1122, 1182, 1242], sent_at)

    def test_a_long_stall_starts_over_rather_than_bursting(self):
        self.assertTrue(self._due(1000))
        self.assertTrue(self._due(1500))     # stalled past several rounds
        self.assertFalse(self._due(1505))    # no catch-up burst
        self.assertTrue(self._due(1560))

    def _run(self, containers, counters):
        self.manager.driver.sample_counters.return_value = counters
        with mock.patch.object(self.manager_mod.objects.Container,
                               'list_by_host', return_value=containers), \
                mock.patch.object(self.manager_mod.notifications,
                                  'notify_metrics') as notify:
            self.manager.report_metrics(self.context)
        return notify

    def test_only_running_containers_are_sampled_and_sent(self):
        running, stopped = FakeContainer(), FakeContainer()
        stopped.uuid, stopped.status = 'stopped-1', 'Stopped'
        notify = self._run([running, stopped], {running.uuid: _reading()})
        sampled = self.manager.driver.sample_counters.call_args.args[1]
        self.assertEqual([running], sampled)
        notify.assert_called_once_with(mock.ANY, running, mock.ANY,
                                       host='compute-1')

    def test_zero_turns_it_off(self):
        self.config(metrics_interval=0, group='usage_report')
        notify = self._run([FakeContainer()], {})
        notify.assert_not_called()
        self.manager.driver.sample_counters.assert_not_called()
