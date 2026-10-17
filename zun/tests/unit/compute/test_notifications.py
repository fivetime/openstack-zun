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


class TestReportingInterval(base.TestCase):
    """The interval has to be read when the task runs, not when it is defined.

    A decorator's arguments are evaluated when the class is defined, which
    is before the config file has been read: `spacing=CONF...` freezes
    whatever the default happened to be, and an operator's setting never
    applies. Measured on a live deployment -- an interval set to 300 kept
    its compiled-in 3600.
    """

    def setUp(self):
        super(TestReportingInterval, self).setUp()
        from zun.compute import manager
        self.manager = manager.Manager.__new__(manager.Manager)
        self.manager._last_report = {}

    def test_the_first_ask_is_always_due(self):
        self.assertTrue(self.manager._due('images', 3600))

    def test_a_second_ask_inside_the_interval_is_not(self):
        self.manager._due('images', 3600)
        self.assertFalse(self.manager._due('images', 3600))

    def test_a_shorter_interval_takes_effect_at_once(self):
        """The point of the fix: the number is read now, not at import."""
        self.manager._due('images', 3600)
        self.assertTrue(self.manager._due('images', 0))

    def test_two_tasks_keep_their_own_clocks(self):
        self.manager._due('images', 3600)
        self.assertTrue(self.manager._due('stale_networks', 3600))


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


def _fake(uuid, container_type, **kw):
    obj = FakeContainer()
    obj.uuid = uuid
    obj.container_type = container_type
    obj.runtime = 'kata-qemu'
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


class TestThreeKinds(base.TestCase):
    """A container, a capsule and a capsule's container never overlap.

    A capsule's allocation is the larger of its containers' sum and each
    init container, not the sum of its children; a reader adding things up
    has to be able to leave the children out, so each kind has its own
    prefix and a child says where its allocation is counted.
    """

    def setUp(self):
        super(TestThreeKinds, self).setUp()
        from zun.common import consts
        self.consts = consts
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        p.start()
        self.addCleanup(p.stop)
        self.child = _fake('cc-1', consts.TYPE_CAPSULE_CONTAINER)
        self.init = _fake('ci-1', consts.TYPE_CAPSULE_INIT_CONTAINER)
        self.capsule = _fake('cap-1', consts.TYPE_CAPSULE,
                             containers=[self.child],
                             init_containers=[self.init])

    def _sent(self):
        return [(c.args[1], c.args[2])
                for c in self.notifier.info.call_args_list]

    def test_each_kind_has_its_own_prefix(self):
        notifications.notify({}, _fake('c-1', self.consts.TYPE_CONTAINER),
                             'start', 'end')
        notifications.notify({}, self.capsule, 'start', 'end')
        notifications.notify({}, self.child, 'start', 'end',
                             extra=None)

        self.assertEqual(['container.start.end', 'capsule.start.end',
                          'capsule.container.start.end'],
                         [e for e, _ in self._sent()])

    def test_a_capsule_names_its_children_and_its_rule(self):
        notifications.notify({}, self.capsule, 'start', 'end')

        payload = self._sent()[0][1]
        self.assertEqual('capsule', payload['container_type'])
        self.assertEqual(['cc-1', 'ci-1'], payload['children'])
        self.assertEqual('max(sum(containers), max(init_containers))',
                         payload['allocation_basis'])

    def test_a_capsule_created_announces_each_child_too(self):
        notifications.notify({}, self.capsule, 'create', 'end')

        sent = {p['uuid']: (e, p) for e, p in self._sent()}
        self.assertEqual('capsule.create.end', sent['cap-1'][0])
        for uuid, kind in (('cc-1', 'capsule_container'),
                           ('ci-1', 'capsule_init_container')):
            event, payload = sent[uuid]
            self.assertEqual('capsule.container.create.end', event)
            self.assertEqual(kind, payload['container_type'])
            self.assertEqual('cap-1', payload['capsule_uuid'])
            self.assertTrue(payload['allocation_included_in_parent'])

    def test_a_capsule_deleted_gives_each_child_its_last_reading(self):
        notifications.notify(
            {}, self.capsule, 'delete', 'end',
            extra={'last_reading': {'cpu': {'total_ns': 9}}},
            children_extra={'cc-1': {'last_reading': {'cpu': {
                'total_ns': 3}}}})

        sent = {p['uuid']: p for _, p in self._sent()}
        self.assertEqual(9, sent['cap-1']['last_reading']['cpu']['total_ns'])
        self.assertEqual(3, sent['cc-1']['last_reading']['cpu']['total_ns'])
        self.assertNotIn('last_reading', sent['ci-1'])

    def test_children_are_not_repeated_for_other_events(self):
        notifications.notify({}, self.capsule, 'stop', 'end')

        self.assertEqual(['capsule.stop.end'], [e for e, _ in self._sent()])

    def test_a_childs_update_finds_its_capsule(self):
        with mock.patch.object(notifications, '_capsule_uuid_of',
                               return_value='cap-1') as find:
            notifications.notify_state_change({}, self.child,
                                              {'status': 'Stopped'})

        event, payload = self._sent()[0]
        self.assertEqual('capsule.container.update', event)
        self.assertEqual('cap-1', payload['capsule_uuid'])
        find.assert_called_once_with({}, self.child)

    def test_an_error_keeps_the_kinds_prefix(self):
        notifications.notify_error({}, self.capsule, 'delete',
                                   RuntimeError('x'))

        self.assertEqual('capsule.delete.error',
                         self.notifier.error.call_args.args[1])


class TestWhenAndHow(base.TestCase):

    def setUp(self):
        super(TestWhenAndHow, self).setUp()
        self.notifier = mock.Mock()
        p = mock.patch.object(notifications.rpc, 'get_notifier',
                              return_value=self.notifier)
        p.start()
        self.addCleanup(p.stop)

    def test_every_payload_says_when_it_happened(self):
        """A message delivered twice then says the same thing twice."""
        notifications.notify({}, FakeContainer(), 'delete', 'end')

        payload = self.notifier.info.call_args.args[2]
        self.assertEqual('2.0', payload['payload_version'])
        self.assertTrue(payload['occurred_at'].endswith('+00:00'))

    def test_record_times_are_iso_utc(self):
        import datetime
        container = FakeContainer()
        container.started_at = datetime.datetime(2026, 10, 17, 1, 2, 3)

        payload = notifications._payload(container)

        self.assertEqual('2026-10-17T01:02:03+00:00', payload['started_at'])

    def test_the_last_reading_goes_on_the_end_only(self):
        with notifications.lifecycle(
                {}, FakeContainer(), 'stop',
                end_extra={'last_reading': {'cpu': {'total_ns': 5}}}):
            pass

        start, end = [c.args[2] for c in self.notifier.info.call_args_list]
        self.assertNotIn('last_reading', start)
        self.assertEqual(5, end['last_reading']['cpu']['total_ns'])


class TestNothingWhenNothingListens(base.TestCase):
    """A deployment without a notification driver pays nothing for one."""

    def test_no_driver_configured_means_no_notifier(self):
        from zun.common import rpc
        with mock.patch.object(rpc, 'NOTIFIER') as notifier:
            notifier.is_enabled.return_value = False
            self.assertIsNone(rpc.get_notifier(service='zun'))

    def test_a_configured_driver_gives_one(self):
        from zun.common import rpc
        with mock.patch.object(rpc, 'NOTIFIER') as notifier:
            notifier.is_enabled.return_value = True
            self.assertIsNotNone(rpc.get_notifier(service='zun'))

    def test_no_reading_is_taken_for_a_message_nobody_gets(self):
        from zun.compute import manager
        mgr = manager.Manager.__new__(manager.Manager)
        mgr.driver = mock.Mock()
        with mock.patch.object(notifications, 'enabled', return_value=False):
            self.assertIsNone(mgr._last_reading({}, FakeContainer()))
        mgr.driver.sample_counters.assert_not_called()

    def test_a_reading_is_taken_when_it_will_be_sent(self):
        from zun.compute import manager
        mgr = manager.Manager.__new__(manager.Manager)
        mgr.driver = mock.Mock()
        mgr.driver.sample_counters.return_value = {
            FakeContainer.uuid: {'cpu': {'total_ns': 7}}}
        with mock.patch.object(notifications, 'enabled', return_value=True):
            found = mgr._last_reading({}, FakeContainer())
        self.assertEqual(7, found['cpu']['total_ns'])

    def test_a_reading_that_fails_does_not_stop_the_operation(self):
        from zun.compute import manager
        mgr = manager.Manager.__new__(manager.Manager)
        mgr.driver = mock.Mock()
        mgr.driver.sample_counters.side_effect = RuntimeError('gone')
        with mock.patch.object(notifications, 'enabled', return_value=True):
            self.assertIsNone(mgr._last_reading({}, FakeContainer()))
