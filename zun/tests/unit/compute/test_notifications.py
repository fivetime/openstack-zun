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
