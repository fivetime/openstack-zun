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

"""top must answer about the container, not about the box around it.

docker answers this from the host, which is right when the container's
processes are host processes. Under a VM runtime the only host process is
the VMM, so docker returns the hypervisor's command line -- handing the
caller the host's memory size, its cpu count and a set of internal paths,
none of which is theirs.
"""

from unittest import mock

from zun.container import driver
from zun.tests import base


class ProcessTableTest(base.TestCase):

    def test_the_shape_the_api_ref_documents(self):
        self.assertEqual(
            {'Titles': ['PID', 'USER', 'COMMAND'],
             'Processes': [['1', 'root', 'sh'], ['9', 'root', 'ps -ef']]},
            driver.process_table('PID   USER     COMMAND\n'
                                 '    1 root     sh\n'
                                 '    9 root     ps -ef\n'))

    def test_the_command_keeps_its_spaces(self):
        table = driver.process_table('PID CMD\n1 sh -c "echo a; echo b"\n')

        self.assertEqual([['1', 'sh -c "echo a; echo b"']],
                         table['Processes'])

    def test_nothing_at_all_is_an_empty_table(self):
        self.assertEqual({'Titles': [], 'Processes': []},
                         driver.process_table(''))


class RuntimeOfTest(base.TestCase):
    """Which runtime a container was actually given."""

    def setUp(self):
        super(RuntimeOfTest, self).setUp()
        from zun.container.docker import driver as docker_driver
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)

    def test_a_containers_own_runtime_wins(self):
        with mock.patch.object(self.driver, '_is_runtime_supported',
                               return_value=True):
            self.assertEqual(
                'kata-qemu',
                self.driver._runtime_of(mock.Mock(runtime='kata-qemu')))

    def test_without_one_the_configured_default_is_used(self):
        with mock.patch.object(self.driver, '_is_runtime_supported',
                               return_value=True), \
                mock.patch('zun.container.docker.driver.CONF') as conf:
            conf.container_runtime = 'kata-qemu'
            self.assertEqual('kata-qemu',
                             self.driver._runtime_of(mock.Mock(runtime=None)))

    def test_a_daemon_that_cannot_take_a_runtime_is_runc(self):
        with mock.patch.object(self.driver, '_is_runtime_supported',
                               return_value=False):
            self.assertEqual(
                'runc',
                self.driver._runtime_of(mock.Mock(runtime='kata-qemu')))
