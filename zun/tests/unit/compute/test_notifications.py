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
