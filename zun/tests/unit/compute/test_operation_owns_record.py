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

"""An operation owns its container's record until it is done.

Found by senlin polling a container every two seconds through a rebuild:
the show path read the runtime mid-rebuild and wrote it back (Running, then
Error on the 404 between the old container and the new one), and a delete
queued behind the rebuild removed the record while the rebuilt container
ran on.
"""

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.compute import manager
import zun.conf
from zun.objects.container import Container
from zun.tests import base
from zun.tests.unit.db import utils


class _ManagerTest(base.TestCase):

    def setUp(self):
        super().setUp()
        p = mock.patch('zun.scheduler.client.report.SchedulerReportClient')
        p.start()
        self.addCleanup(p.stop)
        zun.conf.CONF.set_override('container_driver', 'fake')
        zun.conf.CONF.set_override('capsule_driver', 'fake')
        self.manager = manager.Manager()
        self.container = Container(self.context,
                                   **utils.get_test_container())
        self.fresh = Container(self.context, **utils.get_test_container())
        p = mock.patch.object(Container, 'get_by_uuid',
                              return_value=self.fresh)
        self.get = p.start()
        self.addCleanup(p.stop)


class ShowDuringAnOperationTest(_ManagerTest):

    def _show(self):
        with mock.patch.object(self.manager.driver, 'show',
                               side_effect=self._runtime) as show, \
                mock.patch.object(Container, 'save') as save:
            self.manager.container_show(self.context, self.container)
        return show, save

    def _runtime(self, ctx, container):
        container.status = consts.ERROR
        container.status_reason = '404 Client Error: No such container'
        return container

    def test_a_rebuilding_container_is_not_read_from_the_runtime(self):
        self.container.status = consts.REBUILDING
        self.container.task_state = None    # API set it; compute not yet
        show, save = self._show()
        show.assert_not_called()
        save.assert_not_called()
        self.assertEqual(consts.REBUILDING, self.container.status)

    def test_a_container_with_a_task_is_not_read_from_the_runtime(self):
        self.container.status = consts.CREATED
        self.container.task_state = consts.CONTAINER_REBUILDING
        show, save = self._show()
        show.assert_not_called()
        save.assert_not_called()

    def test_an_idle_container_is_synced(self):
        self.container.status = consts.RUNNING
        self.container.task_state = None
        self.fresh.status, self.fresh.task_state = consts.RUNNING, None
        show, save = self._show()
        show.assert_called_once()
        save.assert_called_once()

    def test_not_written_back_if_an_operation_began_meanwhile(self):
        self.container.status = consts.RUNNING
        self.container.task_state = None
        self.fresh.task_state = consts.CONTAINER_REBUILDING
        show, save = self._show()
        show.assert_called_once()
        save.assert_not_called()


class QueuedOperationCatchesUpTest(_ManagerTest):

    def _delete_after_waiting(self):
        """container_delete's closure, run as if it had waited its turn."""
        ran = []
        with mock.patch.object(manager.utils, 'spawn_n',
                               side_effect=lambda f: f()), \
                mock.patch.object(self.manager, '_do_container_delete',
                                  side_effect=lambda ctx, c, force:
                                  ran.append(c.container_id)), \
                mock.patch.object(manager.notifications, 'notify'):
            self.manager.container_delete(self.context, self.container,
                                          force=True)
        return ran

    def test_a_delete_behind_a_rebuild_deletes_the_rebuilt_container(self):
        self.container.container_id = 'replaced-by-the-rebuild'
        self.container.status = consts.DELETING     # set by the API, unsaved
        self.fresh.container_id = 'made-by-the-rebuild'
        self.fresh.status = consts.RUNNING
        self.assertEqual(['made-by-the-rebuild'],
                         self._delete_after_waiting())
        # The delete still carries out the intent the API recorded.
        self.assertEqual(consts.DELETING, self.container.status)

    def test_nothing_to_do_once_the_record_is_gone(self):
        self.get.side_effect = exception.ContainerNotFound(container='x')
        self.assertEqual([], self._delete_after_waiting())

    def test_every_queued_operation_catches_up_but_create(self):
        import inspect
        src = inspect.getsource(manager.Manager)
        closures = src.count('@utils.synchronized(container.uuid)')
        catching_up = src.count('if not self._caught_up(container):')
        # create makes the record's first runtime object; nothing to catch
        # up with.
        self.assertEqual(closures - 1, catching_up)
