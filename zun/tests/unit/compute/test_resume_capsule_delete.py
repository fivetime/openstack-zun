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

from zun.common import consts
from zun.compute import manager
import zun.conf
from zun import objects
from zun.tests.unit.db import base
from zun.tests.unit.db import utils


class ResumeCapsuleDeleteTest(base.DbTestCase):
    """A delete the caller has its answer for must survive a restart."""

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.manager.capsule_driver = mock.Mock()
        self.capsule = objects.Capsule(
            self.context, **utils.get_test_container())
        p = mock.patch.object(objects.Capsule, 'list_by_host',
                              return_value=[self.capsule])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.manager, 'container_delete')
        self.delete = p.start()
        self.addCleanup(p.stop)

    def test_a_delete_under_way_is_finished(self):
        self.capsule.status = consts.STOPPED
        self.capsule.task_state = consts.CONTAINER_DELETING

        self.manager._resume_capsules(self.context)

        self.delete.assert_called_once_with(self.context, self.capsule,
                                            force=True)
        self.manager.capsule_driver.resume_capsule.assert_not_called()

    def test_a_capsule_marked_deleting_is_finished(self):
        self.capsule.status = consts.DELETING
        self.capsule.task_state = None

        self.manager._resume_capsules(self.context)

        self.delete.assert_called_once_with(self.context, self.capsule,
                                            force=True)

    def test_a_running_capsule_is_left_alone(self):
        self.capsule.status = consts.RUNNING
        self.capsule.task_state = None

        self.manager._resume_capsules(self.context)

        self.delete.assert_not_called()

    def test_one_that_fails_does_not_stop_the_next(self):
        self.capsule.status = consts.DELETING
        self.delete.side_effect = RuntimeError('pool full')

        self.manager._resume_capsules(self.context)

        self.delete.assert_called_once()
