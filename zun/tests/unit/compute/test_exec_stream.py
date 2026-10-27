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

"""API 1.59: the node makes a streamed exec, the API seals its token."""

from unittest import mock
from urllib import parse as urlparse

from zun.common import exception
from zun.compute import api as compute_api
from zun.compute import manager
from zun.compute import rpcapi
from zun.container.cri import driver as cri_driver
from zun.container.docker import driver as docker_driver
from zun.objects.container import Container
from zun.tests import base
from zun.tests.unit.container.fake_driver import FakeDriver as fake_driver
from zun.tests.unit.db import utils
from zun.websocket import channel
from zun.websocket import exec_token

TARGET = {'kind': 'docker', 'url': 'https://10.0.0.5:2376', 'exec_id': 'e1'}


class TestComputeApi(base.TestCase):

    def setUp(self):
        super(TestComputeApi, self).setUp()
        p = mock.patch('zun.scheduler.client.report.SchedulerReportClient')
        p.start()
        self.addCleanup(p.stop)
        self.config(exec_token_key='s3cret', group='websocket_proxy')
        self.container = Container(self.context,
                                   **utils.get_test_container())
        self.container.host = 'node1'

    @mock.patch.object(rpcapi.API, 'container_exec_stream')
    def test_the_token_carries_what_the_proxy_needs(self, rpc):
        rpc.return_value = {'exec_id': 'e1', 'target': dict(TARGET),
                            'proxy_base': 'ws://node1-proxy/'}

        answer = compute_api.API(self.context).container_exec_stream(
            self.context, self.container, 'cat', True, 30)

        url = urlparse.urlparse(answer['proxy_url'])
        self.assertEqual('node1-proxy', url.netloc)
        query = urlparse.parse_qs(url.query)
        self.assertEqual(['exec'], query['stream'])
        self.assertEqual([self.container.uuid], query['uuid'])
        payload = exec_token.open_token(query['token'][0])
        self.assertEqual({'uuid': self.container.uuid, 'host': 'node1',
                          'target': TARGET, 'stdin': True, 'timeout': 30},
                         payload)
        # The runtime's address is in the token only, and sealed.
        self.assertNotIn('10.0.0.5', answer['proxy_url'])
        self.assertEqual(channel.V5, answer['protocol'])
        self.assertEqual(('e1', None, None), (answer['exec_id'],
                                              answer['output'],
                                              answer['exit_code']))

    @mock.patch.object(rpcapi.API, 'container_exec_stream')
    def test_refused_before_anything_is_made(self, rpc):
        self.config(exec_token_key=None, group='websocket_proxy')

        self.assertRaises(exception.Conflict,
                          compute_api.API(self.context).container_exec_stream,
                          self.context, self.container, 'cat', False, None)
        rpc.assert_not_called()


class TestManager(base.TestCase):

    def setUp(self):
        super(TestManager, self).setUp()
        p = mock.patch('zun.scheduler.client.report.SchedulerReportClient')
        p.start()
        self.addCleanup(p.stop)
        self.config(container_driver='fake', capsule_driver='fake')
        self.config(base_url='ws://node1-proxy/', group='websocket_proxy')
        self.manager = manager.Manager()
        self.container = Container(self.context,
                                   **utils.get_test_container())

    @mock.patch.object(fake_driver, 'execute_create_stream', create=True)
    def test_made_and_not_started(self, create):
        create.return_value = dict(TARGET)

        answer = self.manager.container_exec_stream(
            self.context, self.container, 'cat', True)

        create.assert_called_once_with(self.context, self.container, 'cat',
                                       True)
        self.assertEqual({'exec_id': 'e1', 'target': TARGET,
                          'proxy_base': 'ws://node1-proxy/'}, answer)

    @mock.patch.object(fake_driver, 'execute_kill', create=True)
    def test_a_kill_that_fails_is_only_logged(self, kill):
        kill.side_effect = RuntimeError('gone')

        self.manager.container_exec_kill(self.context, 'e1')

        kill.assert_called_once_with('e1')


class TestDrivers(base.TestCase):

    @mock.patch('zun.container.docker.utils.docker_client')
    def test_docker_makes_it_without_a_terminal(self, client):
        docker = client.return_value.__enter__.return_value
        docker.exec_create.return_value = {'Id': 'e1'}
        self.config(docker_remote_api_host='10.0.0.5',
                    docker_remote_api_port='2376', ca_file='ca',
                    cert_file='c', key_file='k', group='docker')
        driver = docker_driver.DockerDriver.__new__(docker_driver.DockerDriver)
        container = mock.Mock(container_id='cid')

        target = driver.execute_create_stream(None, container, 'cat', True)

        docker.exec_create.assert_called_once_with('cid', 'cat', stdin=True,
                                                   tty=False)
        docker.exec_start.assert_not_called()
        self.assertEqual(TARGET, target)

    def test_cri_asks_for_stderr_apart(self):
        driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        driver.runtime_stub = mock.Mock()
        driver.runtime_stub.Exec.return_value = mock.Mock(
            url='http://127.0.0.1:1/exec/t')

        target = driver.execute_create_stream(
            None, mock.Mock(container_id='cid'), ['cat'], False)

        request = driver.runtime_stub.Exec.call_args.args[0]
        self.assertEqual((False, False, True, True),
                         (request.tty, request.stdin, request.stdout,
                          request.stderr))
        self.assertEqual({'kind': 'cri', 'url': 'http://127.0.0.1:1/exec/t',
                          'exec_id': None}, target)
