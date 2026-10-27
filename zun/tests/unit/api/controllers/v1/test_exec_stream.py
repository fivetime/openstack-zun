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

"""API 1.59: execute stream=true answers where to read the command."""

from unittest import mock

from zun.tests.unit.api import base as api_base
from zun.tests.unit.db import utils

ANSWER = {'output': None, 'exit_code': None, 'exec_id': 'e1',
          'proxy_url': 'ws://proxy/?stream=exec&uuid=u&token=t',
          'protocol': 'v5.channel.k8s.io'}


@mock.patch('zun.common.utils.validate_container_state', new=mock.Mock())
class TestContainerExecStream(api_base.FunctionalTest):

    def setUp(self):
        super(TestContainerExecStream, self).setUp()
        self.container = utils.create_test_container(context=self.context,
                                                     status='Running')
        self.base = '/v1/containers/%s/execute?command=cat' % \
            self.container.uuid

    def _post(self, query, version='1.59', **kwargs):
        return self.post(self.base + query, headers={
            'OpenStack-API-Version': 'container %s' % version}, **kwargs)

    @mock.patch('zun.compute.api.API.container_exec')
    @mock.patch('zun.compute.api.API.container_exec_stream')
    def test_answered_at_once_without_waiting(self, stream, execute):
        stream.return_value = dict(ANSWER)

        response = self._post('&run=true&stream=true&stdin=true&timeout=30')

        self.assertEqual(200, response.status_int)
        self.assertEqual(ANSWER, response.json)
        args = stream.call_args.args
        self.assertEqual(('cat', True, 30), args[2:])
        execute.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec_stream')
    def test_without_a_timeout_there_is_none(self, stream):
        stream.return_value = dict(ANSWER)

        self._post('&run=true&stream=true')

        self.assertEqual(('cat', False, None), stream.call_args.args[2:])

    @mock.patch('zun.compute.api.API.container_exec_stream')
    def test_it_needs_1_59(self, stream):
        response = self._post('&run=true&stream=true', version='1.58',
                              expect_errors=True)

        self.assertEqual(400, response.status_int)
        self.assertIn('supported from version 1.59',
                      response.json['errors'][0]['detail'])
        stream.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec_stream')
    def test_not_with_a_terminal_or_detached(self, stream):
        for query in ('&run=true&stream=true&interactive=true',
                      '&run=true&stream=true&detach=true',
                      '&run=false&stream=true',
                      '&run=true&stdin=true'):
            response = self._post(query, expect_errors=True)
            self.assertEqual(400, response.status_int, query)
        stream.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec')
    def test_without_stream_nothing_changes(self, execute):
        execute.return_value = {'output': 'x', 'exit_code': 0,
                                'exec_id': None, 'proxy_url': None}

        response = self._post('&run=true')

        self.assertEqual('x', response.json['output'])
        self.assertNotIn('stream', execute.call_args.kwargs)


@mock.patch('zun.common.utils.validate_container_state', new=mock.Mock())
@mock.patch('zun.api.controllers.v1.capsules.check_policy_on_capsule',
            new=mock.Mock())
class TestCapsuleExecStream(api_base.FunctionalTest):

    def setUp(self):
        super(TestCapsuleExecStream, self).setUp()
        self.target = mock.Mock(name='c', uuid='u')
        p = mock.patch('zun.api.utils.get_resource',
                       return_value=mock.Mock(containers=[self.target],
                                              init_containers=[],
                                              host='node1'))
        p.start()
        self.addCleanup(p.stop)

    def _post(self, query, version='1.59', **kwargs):
        return self.post('/v1/capsules/cap/execute?command=cat' + query,
                         headers={'OpenStack-API-Version':
                                  'container %s' % version}, **kwargs)

    @mock.patch('zun.compute.api.API.container_exec_stream')
    def test_a_capsule_streams_too(self, stream):
        stream.return_value = dict(ANSWER)

        response = self._post('&run=true&stream=true&stdin=true')

        self.assertEqual(ANSWER, response.json)
        target, command, stdin, timeout = stream.call_args.args[1:]
        self.assertIs(self.target, target)
        self.assertEqual('node1', target.host)
        self.assertEqual((['cat'], True, None), (command, stdin, timeout))

    @mock.patch('zun.compute.api.API.container_exec_stream')
    def test_a_timeout_only_with_stream(self, stream):
        response = self._post('&run=true&timeout=5', expect_errors=True)

        self.assertEqual(400, response.status_int)
        stream.assert_not_called()
