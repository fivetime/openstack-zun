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

"""A mount asked for read-only is bound read-only, on both drivers."""

from unittest import mock

from zun.tests import base


class TestReadOnlyBinds(base.TestCase):

    def _volmap(self, read_only):
        volmap = mock.Mock()
        volmap.read_only = read_only
        return volmap

    def test_the_docker_bind_carries_the_mode(self):
        from zun.container.docker import driver

        docker_driver = driver.DockerDriver.__new__(driver.DockerDriver)
        volume_driver = mock.Mock()
        volume_driver.bind_mount.side_effect = [
            ('/host/a', '/a'), ('/host/b', '/b')]
        with mock.patch.object(driver.DockerDriver, '_get_volume_driver',
                               return_value=volume_driver):
            binds = docker_driver._get_binds(
                mock.Mock(), [self._volmap(True), self._volmap(False)])

        self.assertEqual({'/host/a': {'bind': '/a', 'mode': 'ro'},
                          '/host/b': {'bind': '/b', 'mode': 'rw'}}, binds)

    def test_an_attachment_from_before_the_field_is_writable(self):
        from zun.container.docker import driver

        volmap = mock.Mock(spec=[])
        self.assertFalse(driver._read_only(volmap))

    def test_the_cri_mount_carries_readonly(self):
        from zun.container.cri import driver

        cri_driver = driver.CriDriver.__new__(driver.CriDriver)
        volume_driver = mock.Mock()
        volume_driver.bind_mount.return_value = ('/host/a', '/a')
        with mock.patch.object(driver.CriDriver, '_get_volume_driver',
                               return_value=volume_driver):
            mounts = cri_driver._get_mounts(mock.Mock(),
                                            [self._volmap(True)])

        self.assertEqual(1, len(mounts))
        self.assertTrue(mounts[0].readonly)
        self.assertEqual('/a', mounts[0].container_path)
