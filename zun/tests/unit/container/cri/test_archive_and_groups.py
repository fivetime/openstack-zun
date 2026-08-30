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

"""`docker cp` and security groups on the CRI driver.

Neither exists in the CRI. A container's security groups live on its
neutron ports, which this driver made itself, so it edits them itself.
A copy has no host path to go through -- a kata container's filesystem
is inside the virtual machine -- so both directions run tar inside it.
"""

from unittest import mock

from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


class SecurityGroupsOnPortsTest(base.TestCase):

    def setUp(self):
        super(SecurityGroupsOnPortsTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.container = mock.Mock(uuid='u-1', addresses={
            'net-a': [{'port': 'port-1'}],
            'net-b': [{'port': 'port-2'}, {'port': 'port-2'}]})

    def _run(self, add, existing=None):
        api = mock.Mock()
        api.get_neutron_port.return_value = {
            'id': 'port-1', 'security_groups': list(existing or [])}
        with mock.patch.object(cri_driver.neutron, 'NeutronAPI',
                               return_value=api):
            with mock.patch.object(cri_driver.utils, 'get_security_group_ids',
                                   return_value=['sg-1']):
                self.driver._change_security_groups(
                    {}, self.container, 'web', add=add)
        return api

    def test_every_port_is_edited_once(self):
        api = self._run(add=True)

        self.assertEqual(2, api.update_port.call_count)

    def test_adding_keeps_what_was_there(self):
        api = self._run(add=True, existing=['sg-0'])
        sent = api.update_port.call_args.args[1]['port']['security_groups']

        self.assertEqual(['sg-0', 'sg-1'], sent)

    def test_adding_twice_does_not_duplicate(self):
        api = self._run(add=True, existing=['sg-1'])
        sent = api.update_port.call_args.args[1]['port']['security_groups']

        self.assertEqual(['sg-1'], sent)

    def test_removing_takes_only_that_one(self):
        api = self._run(add=False, existing=['sg-0', 'sg-1'])
        sent = api.update_port.call_args.args[1]['port']['security_groups']

        self.assertEqual(['sg-0'], sent)

    def test_a_container_with_no_port_is_refused(self):
        self.container.addresses = {}

        self.assertRaises(exception.ZunException,
                          self.driver._change_security_groups,
                          {}, self.container, 'web', True)


class ArchiveThroughTarTest(base.TestCase):

    def setUp(self):
        super(ArchiveThroughTarTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.container = mock.Mock(uuid='u-1', container_id='c-1')

    def test_reading_runs_tar_in_the_right_directory(self):
        with mock.patch.object(self.driver, '_stat_in_container',
                               return_value={'name': 'c'}):
            with mock.patch.object(self.driver, '_exec_in_container',
                                   return_value=(0, b'TAR', b'')) as ran:
                data, stat = self.driver.get_archive({}, self.container,
                                                     '/a/b/c')

        self.assertEqual(b'TAR', data)
        self.assertEqual({'name': 'c'}, stat)
        self.assertEqual(['tar', '-cf', '-', '-C', '/a/b', 'c'],
                         ran.call_args.args[1])

    def test_a_failed_read_is_not_returned_as_an_archive(self):
        """Half a tar restores as a corrupt tree; say so instead."""
        with mock.patch.object(self.driver, '_stat_in_container',
                               return_value={}):
            with mock.patch.object(self.driver, '_exec_in_container',
                                   return_value=(1, b'', b'no such file')):
                self.assertRaises(exception.Invalid, self.driver.get_archive,
                                  {}, self.container, '/a/b/c')

    def test_stat_reads_the_fields_docker_reports(self):
        with mock.patch.object(self.driver, '_exec_in_container',
                               return_value=(0, b"12|81a4|1700000000|'/x'\n",
                                             b'')):
            stat = self.driver._stat_in_container(self.container, '/x')

        self.assertEqual(12, stat['size'])
        self.assertEqual(0o100644, stat['mode'])
        self.assertEqual('x', stat['name'])

    def test_a_missing_path_is_refused_rather_than_guessed(self):
        with mock.patch.object(self.driver, '_exec_in_container',
                               return_value=(1, b'', b'')):
            self.assertRaises(exception.Invalid,
                              self.driver._stat_in_container,
                              self.container, '/x')

    def test_writing_sends_the_archive_to_the_streams_stdin(self):
        with mock.patch.object(self.driver, '_streaming_exec_url',
                               return_value='wss://node/exec') as url:
            with mock.patch.object(cri_driver.stream,
                                   'write_stdin') as wrote:
                self.driver.put_archive({}, self.container, '/dest', b'TAR')

        self.assertEqual(['tar', '-xf', '-', '-C', '/dest'],
                         url.call_args.args[1])
        self.assertTrue(url.call_args.kwargs['stdin'])
        self.assertEqual('wss://node/exec', wrote.call_args.args[0])
        self.assertEqual(b'TAR', wrote.call_args.args[1])
