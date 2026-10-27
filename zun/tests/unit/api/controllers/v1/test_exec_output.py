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

"""API 1.58: a cut or altered exec output is said so, to every version."""

from unittest import mock

from zun.container import exec_output
from zun.tests.unit.api import base as api_base
from zun.tests.unit.db import utils


def _reply(stdout=b'', stderr=b'', limit=8):
    """What a compute node answers, made the way it makes it."""
    output = exec_output.ExecOutput(limit=limit)
    output.feed(exec_output.STDOUT, stdout)
    output.feed(exec_output.STDERR, stderr)
    reply = exec_output.client_fields(output.as_wire())
    reply.update({'exit_code': 0, 'exec_id': None, 'proxy_url': None})
    return reply


@mock.patch('zun.common.utils.validate_container_state', new=mock.Mock())
class TestContainerExecOutput(api_base.FunctionalTest):

    def setUp(self):
        super(TestContainerExecOutput, self).setUp()
        self.container = utils.create_test_container(context=self.context,
                                                     status='Running')
        self.url = ('/v1/containers/%s/execute?command=x'
                    % self.container.uuid)

    def _post(self, query, version):
        return self.post(self.url + query, headers={
            'OpenStack-API-Version': 'container %s' % version})

    @mock.patch('zun.compute.api.API.container_exec')
    def test_an_old_version_is_told_by_header(self, execute):
        execute.return_value = _reply(stdout=b'0123456789', stderr=b'e')

        response = self._post('&run=true', '1.40')

        self.assertEqual('stdout',
                         response.headers['X-Zun-Output-Truncated'])
        # output is what it always was: one string
        self.assertEqual('01234567e', response.json['output'])

    @mock.patch('zun.compute.api.API.container_exec')
    def test_1_58_reads_the_streams_apart(self, execute):
        execute.return_value = _reply(stdout=b'out', stderr=b'\xff')

        response = self._post('&run=true', '1.58')

        self.assertEqual('out', response.json['stdout'])
        self.assertEqual('/w==', response.json['stderr'])
        self.assertEqual('base64', response.json['stderr_encoding'])
        self.assertEqual([], response.json['output_truncated'])
        self.assertEqual('stderr',
                         response.headers['X-Zun-Output-Replaced'])
        self.assertNotIn('X-Zun-Output-Truncated', response.headers)

    @mock.patch('zun.compute.api.API.container_exec')
    def test_an_interactive_session_is_answered_as_before(self, execute):
        session = {'output': None, 'exit_code': None, 'exec_id': 'e1',
                   'proxy_url': 'ws://proxy/?token=t'}
        execute.return_value = dict(session)

        response = self._post('&run=false&interactive=true', '1.40')

        self.assertEqual(session, response.json)
        self.assertNotIn('X-Zun-Output-Truncated', response.headers)
        self.assertNotIn('X-Zun-Output-Replaced', response.headers)


@mock.patch('zun.common.utils.validate_container_state', new=mock.Mock())
@mock.patch('zun.api.controllers.v1.capsules.check_policy_on_capsule',
            new=mock.Mock())
class TestCapsuleExecOutput(api_base.FunctionalTest):

    @mock.patch('zun.compute.api.API.container_exec')
    @mock.patch('zun.api.utils.get_resource')
    def test_the_capsule_endpoint_says_so_too(self, get_resource, execute):
        target = mock.Mock(name='c', uuid='u')
        get_resource.return_value = mock.Mock(
            containers=[target], init_containers=[], host='node1')
        execute.return_value = _reply(stderr=b'x' * 20)

        response = self.post(
            '/v1/capsules/cap/execute?command=x&run=true',
            headers={'OpenStack-API-Version': 'container 1.40'})

        self.assertEqual('stderr',
                         response.headers['X-Zun-Output-Truncated'])
        self.assertEqual('xxxxxxxx', response.json['output'])
