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

"""A failed probe restarts its container -- unless the owner got there first.

The probe sweep works from the records it loaded when it began. A delete
or a stop arriving meanwhile runs in a thread of its own, and a restart
then would create a container in a sandbox that is on its way out. The
restart is decided under the capsule's lock, which a delete holds for its
whole length, on a fresh read.
"""

import threading
import time
from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.common import utils
from zun.container.cri import driver as cri_driver
from zun.tests import base


class RestartForProbeTest(base.TestCase):

    def setUp(self):
        super().setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.capsule = mock.Mock(uuid='capsule-1')
        self.container = mock.Mock(uuid='member-1', container_id='cid')
        self.fresh = {
            'capsule-1': mock.Mock(uuid='capsule-1', status=consts.RUNNING,
                                   task_state=None),
            'member-1': mock.Mock(uuid='member-1', status=consts.RUNNING,
                                  task_state=None),
        }
        p = mock.patch.object(
            cri_driver.objects.Container, 'get_container_any_type',
            side_effect=lambda ctx, uuid: self.fresh[uuid])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.driver, '_restart_container',
                              return_value=True)
        self.restart = p.start()
        self.addCleanup(p.stop)

    def _probe_restart(self):
        return self.driver._restart_for_probe(
            self.context, self.capsule, self.container)

    def test_restarts_a_running_container(self):
        self.assertTrue(self._probe_restart())
        self.restart.assert_called_once_with(
            self.context, self.capsule, self.container)

    def test_not_while_the_capsule_is_being_deleted(self):
        self.fresh['capsule-1'].task_state = consts.CONTAINER_DELETING
        self.assertFalse(self._probe_restart())
        self.restart.assert_not_called()

    def test_not_once_the_container_was_stopped(self):
        self.fresh['member-1'].status = consts.STOPPED
        self.assertFalse(self._probe_restart())
        self.restart.assert_not_called()

    def test_not_when_the_capsule_is_gone(self):
        self.fresh = {}

        def gone(ctx, uuid):
            raise exception.ContainerNotFound(container=uuid)
        cri_driver.objects.Container.get_container_any_type.side_effect = gone
        self.assertFalse(self._probe_restart())
        self.restart.assert_not_called()

    def test_waits_for_a_delete_in_progress(self):
        # The delete holds the capsule's lock (utils.synchronized on its
        # uuid, as the compute manager takes it) and, when it lets go, the
        # capsule is gone: the probe must decide on that, not on what the
        # sweep read before.
        inside, release = threading.Event(), threading.Event()

        @utils.synchronized('capsule-1')
        def delete():
            inside.set()
            release.wait(5)
            self.fresh['capsule-1'].status = consts.DELETED

        deleting = threading.Thread(target=delete)
        deleting.start()
        inside.wait(5)
        answer = []
        probing = threading.Thread(
            target=lambda: answer.append(self._probe_restart()))
        probing.start()
        time.sleep(0.2)
        self.assertEqual([], answer)    # still waiting for the delete
        release.set()
        deleting.join(5)
        probing.join(5)

        self.assertEqual([False], answer)
        self.restart.assert_not_called()
