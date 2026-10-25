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

"""API 1.57: an exec's timeout through the RPC client, manager and drivers."""

import socket
from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.compute import manager
from zun.compute import rpcapi
import zun.conf
from zun.container.cri import driver as cri_driver
from zun.container.docker import driver as docker_driver
from zun import objects
from zun.tests import base
from zun.tests.unit.db import utils


class TestRpcWaitsAsLongAsTheCommand(base.TestCase):

    def setUp(self):
        super(TestRpcWaitsAsLongAsTheCommand, self).setUp()
        service = mock.Mock(host='h')
        for patcher in (
                mock.patch.object(rpcapi.objects.ZunService, 'list_by_binary',
                                  return_value=[service]),
                mock.patch.object(rpcapi.servicegroup, 'ServiceGroup')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.api = rpcapi.API.__new__(rpcapi.API)
        self.api._call = mock.Mock()
        self.api._call_bounded = mock.Mock()
        self.container = mock.Mock(host='h')

    def test_the_deadline_is_the_command_s_plus_a_margin(self):
        self.api.container_exec(None, self.container, 'c', True, False,
                                timeout=240)

        args, kwargs = self.api._call_bounded.call_args
        self.assertEqual(('h', 'container_exec',
                          240 + rpcapi.EXEC_REPLY_MARGIN), args)
        self.assertEqual(240, kwargs['timeout'])
        self.api._call.assert_not_called()

    def test_the_margin_outlasts_the_node_s_own(self):
        """Else the caller hears 'the node stopped answering' instead."""
        self.assertGreater(rpcapi.EXEC_REPLY_MARGIN,
                           cri_driver.EXEC_REPLY_MARGIN)

    def test_without_one_the_call_is_the_one_it_always_was(self):
        self.api.container_exec(None, self.container, 'c', True, False)

        self.assertNotIn('timeout', self.api._call.call_args.kwargs)
        self.api._call_bounded.assert_not_called()


class TestManagerPassesItOn(base.TestCase):

    def setUp(self):
        super(TestManagerPassesItOn, self).setUp()
        p = mock.patch('zun.scheduler.client.report.SchedulerReportClient')
        p.start()
        self.addCleanup(p.stop)
        zun.conf.CONF.set_override('container_driver', 'fake')
        zun.conf.CONF.set_override('capsule_driver', 'fake')
        self.manager = manager.Manager()
        self.driver = mock.Mock()
        self.driver.execute_create.return_value = 'e1'
        self.driver.execute_run.return_value = (b'ok', 0)
        self.container = objects.Container(
            self.context, **utils.get_test_container(status=consts.RUNNING))

    def _exec(self, **kwargs):
        with mock.patch.object(self.manager, '_get_driver',
                               return_value=self.driver):
            return self.manager.container_exec(
                self.context, self.container, 'c', True, False, **kwargs)

    def test_to_the_driver(self):
        self._exec(timeout=240)

        self.driver.execute_run.assert_called_once_with('e1', 'c',
                                                        timeout=240)

    def test_and_not_at_all_when_not_asked(self):
        self._exec()

        self.driver.execute_run.assert_called_once_with('e1', 'c')


class TestDockerDriverWaitsAsAsked(base.TestCase):

    def setUp(self):
        super(TestDockerDriverWaitsAsAsked, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)
        docker = mock.MagicMock()
        docker.exec_start.return_value = 'sock'
        docker.exec_inspect.return_value = {'ExitCode': 0}
        client = mock.patch.object(docker_driver.docker_utils,
                                   'docker_client')
        client.start().return_value.__enter__.return_value = docker
        self.addCleanup(client.stop)

    @mock.patch('zun.container.docker.utils.read_exec_output',
                return_value=b'ok')
    def test_the_asked_timeout_is_the_one_kept(self, read):
        self.driver.execute_run('e1', 'c', timeout=240)

        read.assert_called_once_with('sock', 240)

    @mock.patch('zun.container.docker.utils.read_exec_output',
                return_value=b'ok')
    def test_the_configured_one_otherwise(self, read):
        self.driver.execute_run('e1', 'c')

        read.assert_called_once_with('sock',
                                     zun.conf.CONF.docker.execute_timeout)

    @mock.patch('zun.container.docker.utils.read_exec_output',
                side_effect=socket.timeout())
    def test_running_out_says_how_long_it_waited(self, read):
        e = self.assertRaises(exception.Conflict, self.driver.execute_run,
                              'e1', 'sleep 900', timeout=240)

        self.assertIn('within 240 seconds', str(e))


class TestCriDriverWaitsAsAsked(base.TestCase):

    def setUp(self):
        super(TestCriDriverWaitsAsAsked, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.run = mock.patch.object(self.driver, '_exec_in_container',
                                     return_value=(0, b'ok', b'')).start()
        self.addCleanup(mock.patch.stopall)

    def test_the_asked_timeout_is_the_one_kept(self):
        self.driver.execute_run('e1', ['c'], timeout=240)

        self.run.assert_called_once_with('e1', ['c'], 240)

    def test_the_configured_one_otherwise(self):
        self.driver.execute_run('e1', ['c'])

        self.run.assert_called_once_with('e1', ['c'],
                                         zun.conf.CONF.cri_exec_timeout)
