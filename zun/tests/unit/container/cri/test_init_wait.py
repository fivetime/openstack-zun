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

"""Waiting on an init container, and what can end the wait.

The create holds the capsule's lock throughout, so a delete used to queue
behind an init container that did not exit, for as long as the wait lasted.
"""

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


class InitWaitTest(base.TestCase):

    def setUp(self):
        super(InitWaitTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver._abandoned = set()
        self.context = mock.Mock()
        self.init = mock.Mock(uuid='i-1', status_detail=None)
        self.init.name = 'init-1'
        self.main = mock.Mock(uuid='m-1')
        self.capsule = mock.Mock(uuid='c-1', container_id=None,
                                 addresses=None, containers=[self.main],
                                 init_containers=[self.init])
        self.create_container = self._patch('_create_container')
        self._patch('_create_pod_sandbox')
        self._patch('_record_start')
        self.undo = self._patch('_undo_create')
        self.show = self._patch('_show_container')
        sleep = mock.patch('tenacity.nap.time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def _patch(self, name, **kwargs):
        patcher = mock.patch.object(cri_driver.CriDriver, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _create(self):
        return self.driver.create_capsule(self.context, self.capsule, {},
                                          [{'network': 'net-1'}], {})

    def _exits(self, detail):
        answer = mock.Mock(status=consts.STOPPED, status_detail=detail)

        def show(context, container):
            container.status_detail = detail
            return answer
        self.show.side_effect = show
        return answer

    def test_a_delete_ends_the_wait_and_undoes_the_create(self):
        def running_then_deleted(context, container):
            if self.show.call_count == 3:
                self.driver.abandon_create('c-1')
            return mock.Mock(status=consts.RUNNING)
        self.show.side_effect = running_then_deleted

        self.assertRaises(exception.CapsuleCreateAbandoned, self._create)

        self.assertEqual(3, self.show.call_count)
        self.undo.assert_called_once_with(self.context, self.capsule)
        self.assertEqual([mock.call(self.context, self.capsule, self.init,
                                    {})],
                         self.create_container.call_args_list)

    def test_a_delete_of_another_capsule_does_not(self):
        self.driver.abandon_create('c-2')
        self.show.return_value = self._exits('exit:0')
        self._create()
        self.assertEqual(2, self.create_container.call_count)

    def test_a_delete_stops_the_next_member_from_being_made(self):
        # A delete arriving just after the wait's last look.
        self.driver._wait_for_init_container = mock.Mock(
            side_effect=lambda *a: self.driver.abandon_create('c-1'))
        self.assertRaises(exception.CapsuleCreateAbandoned, self._create)
        self.assertEqual(1, self.create_container.call_count)

    def test_forgotten_after_the_delete(self):
        self.driver.abandon_create('c-1')
        self.driver.forget_abandoned('c-1')
        self.show.return_value = self._exits('exit:0')
        self._create()
        self.assertEqual(2, self.create_container.call_count)

    def test_polling_is_capped(self):
        count = 12
        answers = [mock.Mock(status=consts.RUNNING)] * count
        answers.append(self._exits('exit:0'))
        self.show.side_effect = answers
        self._create()
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(count, len(waits))
        self.assertEqual(cri_driver.INIT_CONTAINER_POLL_MAX, max(waits))

    def test_a_failed_init_fails_the_create(self):
        self.show.return_value = self._exits('exit:1')
        exc = self.assertRaises(exception.ZunException, self._create)
        self.assertIn('init-1', str(exc))
        self.assertIn('exit:1', str(exc))
        self.create_container.assert_called_once()
        self.undo.assert_called_once()

    def test_a_clean_init_goes_on(self):
        self.show.return_value = self._exits('exit:0')
        self._create()
        self.assertEqual(2, self.create_container.call_count)
        self.undo.assert_not_called()

    def test_an_unread_exit_is_not_a_failure(self):
        self.show.return_value = self._exits(None)
        self._create()
        self.assertEqual(2, self.create_container.call_count)
