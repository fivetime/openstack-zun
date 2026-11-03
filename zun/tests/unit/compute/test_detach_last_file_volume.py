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

import os
import shutil
import tempfile
from unittest import mock

from oslo_utils import uuidutils

from zun.compute import manager
import zun.conf
from zun import objects
from zun.tests.unit.db import base
from zun.tests.unit.db import utils
from zun.volume import driver as volume_driver


class _LocalOnlyDriver(object):
    """The container driver's volume calls, down to the real Local driver."""

    def __init__(self):
        self.local = volume_driver.Local()
        self.deleted = []

    def detach_volume(self, context, volmap):
        self.local.detach(context, volmap)

    def delete_volume(self, context, volmap):
        self.deleted.append(volmap.uuid)
        self.local.delete(context, volmap)

    def is_volume_deleted(self, context, volmap):
        return True, False


class DetachLastFileVolumeTest(base.DbTestCase):
    """Deleting what holds a project's last volume without a Cinder id.

    Real rows and a real directory: the failure lives in what a NULL
    cinder_volume_id matches in the database and in what is left on disk
    by the time the second removal runs.
    """

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.driver = _LocalOnlyDriver()
        p = mock.patch.object(self.manager, '_get_driver',
                              return_value=self.driver)
        p.start()
        self.addCleanup(p.stop)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        p = mock.patch('zun.common.mount.get_mountpoint',
                       side_effect=lambda name: os.path.join(self.root, name))
        p.start()
        self.addCleanup(p.stop)
        self.container = mock.Mock(uuid=uuidutils.generate_uuid())

    def _file_volume(self, container_uuid=None):
        volume = utils.create_test_volume(
            context=self.context, uuid=uuidutils.generate_uuid(),
            cinder_volume_id=None, volume_provider='local',
            auto_remove=True, project_id=self.context.project_id)
        utils.create_test_volume_mapping(
            context=self.context, uuid=uuidutils.generate_uuid(),
            volume_id=volume.id, project_id=self.context.project_id,
            container_uuid=container_uuid or self.container.uuid)
        path = os.path.join(self.root, volume.uuid)
        os.makedirs(path)
        return path

    def _mappings_left(self):
        return objects.VolumeMapping.list_by_container(
            self.context, self.container.uuid)

    def test_last_in_the_project(self):
        path = self._file_volume()

        self.manager._detach_volumes(self.context, self.container)

        self.assertFalse(os.path.exists(path))
        self.assertEqual([], self._mappings_left())

    def test_last_in_the_project_when_forced(self):
        # force is what an operator reaches for when a delete is stuck.
        path = self._file_volume()

        self.manager._detach_volumes(self.context, self.container,
                                     reraise=False)

        self.assertFalse(os.path.exists(path))
        self.assertEqual([], self._mappings_left())

    def test_every_volume_of_the_last_holder(self):
        paths = [self._file_volume(), self._file_volume()]

        self.manager._detach_volumes(self.context, self.container)

        self.assertFalse(any(os.path.exists(p) for p in paths))
        self.assertEqual([], self._mappings_left())

    def test_with_another_holder_in_the_project(self):
        other = self._file_volume(container_uuid=uuidutils.generate_uuid())
        path = self._file_volume()

        self.manager._detach_volumes(self.context, self.container)

        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(other))

    def test_a_file_volume_is_only_detached(self):
        # Detach removes all there is; there is no Cinder volume to delete.
        self._file_volume()

        self.manager._detach_volumes(self.context, self.container)

        self.assertEqual([], self.driver.deleted)

    def test_directory_already_gone(self):
        # Failing here kept the mapping, so a retry failed the same way and
        # the record could never be deleted.
        path = self._file_volume()
        shutil.rmtree(path)

        self.manager._detach_volumes(self.context, self.container)

        self.assertEqual([], self._mappings_left())


class DetachCinderVolumeTest(base.DbTestCase):
    """What the count is for: a Cinder volume goes with its last mapping."""

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.driver = mock.Mock()
        self.driver.is_volume_deleted.return_value = (True, False)
        p = mock.patch.object(self.manager, '_get_driver',
                              return_value=self.driver)
        p.start()
        self.addCleanup(p.stop)
        self.container = mock.Mock(uuid=uuidutils.generate_uuid())
        self.cinder_id = uuidutils.generate_uuid()

    def _map(self, container_uuid):
        volume = utils.create_test_volume(
            context=self.context, uuid=uuidutils.generate_uuid(),
            cinder_volume_id=self.cinder_id, volume_provider='cinder',
            auto_remove=True, project_id=self.context.project_id)
        utils.create_test_volume_mapping(
            context=self.context, uuid=uuidutils.generate_uuid(),
            volume_id=volume.id, project_id=self.context.project_id,
            container_uuid=container_uuid)

    def test_last_mapping_deletes_the_volume(self):
        self._map(self.container.uuid)

        self.manager._detach_volumes(self.context, self.container)

        self.driver.detach_volume.assert_called_once()
        self.driver.delete_volume.assert_called_once()

    def test_another_mapping_keeps_the_volume(self):
        self._map(self.container.uuid)
        self._map(uuidutils.generate_uuid())

        self.manager._detach_volumes(self.context, self.container)

        self.driver.detach_volume.assert_called_once()
        self.driver.delete_volume.assert_not_called()
