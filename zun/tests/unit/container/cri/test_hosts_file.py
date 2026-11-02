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

"""A capsule's /etc/hosts says nothing about the node it is on."""

import os
import shutil
import tempfile
from unittest import mock

from zun.container.cri import driver as cri_driver
from zun.container.cri import hostsfile
from zun.criapi import api_pb2
from zun.tests import base

ADDRESSES = {'net-1': [{'addr': '192.168.1.20', 'version': 4},
                       {'addr': '2400::1', 'version': 6}]}


class HostsFileTest(base.TestCase):

    def setUp(self):
        super(HostsFileTest, self).setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def _read(self, path):
        with open(path) as fd:
            return fd.read()

    def test_it_names_the_sandbox_and_its_addresses_only(self):
        path = hostsfile.write(self.root, 'c-1', 'web',
                               hostsfile.own_addresses(ADDRESSES))

        text = self._read(path)
        self.assertIn('192.168.1.20\tweb\n', text)
        self.assertIn('2400::1\tweb\n', text)
        self.assertIn('127.0.0.1\tlocalhost\n', text)
        self.assertEqual(8, len(text.splitlines()))

    def test_a_name_that_could_add_a_line_is_left_out(self):
        text = hostsfile.render('web\n10.0.0.1 evil', ['192.168.1.20'])

        self.assertNotIn('evil', text)
        self.assertNotIn('192.168.1.20', text)

    def test_no_address_yet_is_still_a_file(self):
        self.assertIn('localhost', hostsfile.render('web', []))
        self.assertEqual([], hostsfile.own_addresses(None))

    def test_the_same_content_keeps_the_same_file(self):
        """Members already running hold it by inode."""
        path = hostsfile.write(self.root, 'c-1', 'web', ['192.168.1.20'])
        inode = os.stat(path).st_ino

        hostsfile.write(self.root, 'c-1', 'web', ['192.168.1.20'])

        self.assertEqual(inode, os.stat(path).st_ino)

    def test_new_addresses_are_written(self):
        path = hostsfile.write(self.root, 'c-1', 'web', ['192.168.1.20'])

        hostsfile.write(self.root, 'c-1', 'web', ['192.168.1.21'])

        self.assertIn('192.168.1.21', self._read(path))
        self.assertEqual(['c-1.hosts'], os.listdir(self.root))

    def test_removing_what_is_not_there_is_fine(self):
        hostsfile.remove(self.root, 'never-made')


class HostsMountTest(base.TestCase):

    def setUp(self):
        super(HostsMountTest, self).setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        cri_driver.CONF.set_override('cri_hosts_root', self.root)
        self.drv = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.capsule = mock.Mock(uuid='c-1', hostname=None,
                                 addresses=ADDRESSES)

    def test_a_mount_stops_the_runtime_copying_the_nodes_file(self):
        mounts = []

        self.drv._add_hosts_mount(self.capsule, mounts, False)

        self.assertEqual('/etc/hosts', mounts[0].container_path)
        self.assertEqual(os.path.join(self.root, 'c-1.hosts'),
                         mounts[0].host_path)
        self.assertFalse(mounts[0].readonly)

    def test_a_read_only_root_gets_it_read_only(self):
        mounts = []

        self.drv._add_hosts_mount(self.capsule, mounts, True)

        self.assertTrue(mounts[0].readonly)

    def test_a_volume_asked_for_there_is_left_alone(self):
        mine = api_pb2.Mount(container_path='/etc/hosts', host_path='/mine')
        mounts = [mine]

        self.drv._add_hosts_mount(self.capsule, mounts, False)

        self.assertEqual(['/mine'], [m.host_path for m in mounts])
        self.assertEqual([], os.listdir(self.root))

    def test_a_file_that_cannot_be_written_fails_the_create(self):
        """Going on would give the container the node's file."""
        with mock.patch.object(hostsfile, 'write', side_effect=OSError):
            self.assertRaises(OSError, self.drv._add_hosts_mount,
                              self.capsule, [], False)

    def test_a_delete_removes_it(self):
        self.drv._add_hosts_mount(self.capsule, [], False)
        capsule = mock.Mock(uuid='c-1', containers=[], init_containers=[])

        self.drv._remove_files_of(capsule)

        self.assertEqual([], os.listdir(self.root))


class ContainerConfigTest(base.TestCase):
    """The mount has to reach what the runtime is sent."""

    def test_every_container_is_given_the_file(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        cri_driver.CONF.set_override('cri_hosts_root', root)
        drv = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        capsule = mock.Mock(uuid='c-1', hostname=None, addresses=ADDRESSES)
        container = mock.Mock(
            uuid='m-1', name='main', image='alpine', command=None,
            environment=None, workdir=None, labels=None, entrypoint=None,
            tty=False, interactive=False, cpu=None, memory=None, swap=None,
            restart_policy={}, annotations=None, healthcheck=None)
        container.name = 'main'

        with mock.patch.object(drv, '_probe_helper_mount',
                               return_value=None), \
                mock.patch.object(drv, '_snapshot_annotations',
                                  return_value={}), \
                mock.patch.object(cri_driver, '_asked', return_value=None), \
                mock.patch.object(
                    cri_driver, '_linux_security_context',
                    return_value=api_pb2.LinuxContainerSecurityContext()):
            config = drv._get_container_config(None, capsule, container, {})

        self.assertEqual(['/etc/hosts'],
                         [m.container_path for m in config.mounts])
