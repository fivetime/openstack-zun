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

"""API 1.57: how long execute waits for a command, as the caller asks."""

from unittest import mock

from oslo_config import fixture as config_fixture

from zun import conf
from zun.tests.unit.api import base as api_base
from zun.tests.unit.db import utils


@mock.patch('zun.common.utils.validate_container_state', new=mock.Mock())
class TestExecTimeout(api_base.FunctionalTest):

    V157 = {'OpenStack-API-Version': 'container 1.57'}

    def setUp(self):
        super(TestExecTimeout, self).setUp()
        self.container = utils.create_test_container(context=self.context,
                                                     status='Running')
        self.base = '/v1/containers/%s/execute?command=x' % self.container.uuid

    def _post(self, query, version=None, **kwargs):
        headers = {'OpenStack-API-Version': 'container %s' % version} \
            if version else dict(self.V157)
        return self.post(self.base + query, headers=headers, **kwargs)

    @mock.patch('zun.compute.api.API.container_exec')
    def test_the_timeout_is_passed_on(self, execute):
        execute.return_value = {'output': 'ok', 'exit_code': 0,
                                'exec_id': None, 'proxy_url': None}
        response = self._post('&run=true&timeout=240')

        self.assertEqual(200, response.status_int)
        self.assertEqual(240, execute.call_args.kwargs['timeout'])

    @mock.patch('zun.compute.api.API.container_exec')
    def test_without_one_the_call_is_as_before(self, execute):
        """Not even an empty argument, so an old compute node still takes it.
        """
        execute.return_value = {'output': 'ok', 'exit_code': 0,
                                'exec_id': None, 'proxy_url': None}
        self._post('&run=true')

        self.assertNotIn('timeout', execute.call_args.kwargs)

    @mock.patch('zun.compute.api.API.container_exec')
    def test_it_needs_1_57(self, execute):
        response = self._post('&run=true&timeout=60', version='1.56',
                              expect_errors=True)

        self.assertEqual(400, response.status_int)
        self.assertIn('supported from version 1.57',
                      response.json['errors'][0]['detail'])
        execute.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec')
    def test_the_operator_sets_the_ceiling(self, execute):
        self.useFixture(config_fixture.Config(conf.CONF)).config(
            max_execute_timeout=120, group='api')

        response = self._post('&run=true&timeout=121', expect_errors=True)

        self.assertEqual(400, response.status_int)
        self.assertIn('between 1 and 120 seconds',
                      response.json['errors'][0]['detail'])
        execute.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec')
    def test_the_ceiling_itself_is_allowed(self, execute):
        execute.return_value = {'output': 'ok', 'exit_code': 0,
                                'exec_id': None, 'proxy_url': None}
        response = self._post('&run=true&timeout=%d'
                              % conf.CONF.api.max_execute_timeout)

        self.assertEqual(200, response.status_int)

    @mock.patch('zun.compute.api.API.container_exec')
    def test_zero_and_words_are_not_timeouts(self, execute):
        for value in ('0', 'soon', '-5'):
            response = self._post('&run=true&timeout=' + value,
                                  expect_errors=True)
            self.assertEqual(400, response.status_int, value)
        execute.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec')
    def test_only_a_command_that_is_waited_for_has_one(self, execute):
        for query in ('&run=true&detach=true&timeout=60',
                      '&run=true&interactive=true&timeout=60',
                      '&run=false&timeout=60'):
            response = self._post(query, expect_errors=True)
            self.assertEqual(400, response.status_int, query)
        execute.assert_not_called()
