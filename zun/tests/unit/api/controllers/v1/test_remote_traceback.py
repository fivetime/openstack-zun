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

"""An error raised on a compute node reaches the caller without its trace."""

import sys
from unittest import mock

from oslo_messaging._drivers import common as msg_common

from zun.common import exception
from zun.tests.unit.api import base as api_base
from zun.tests.unit.db import utils


def _as_received(exc):
    """`exc` raised on a node, as the API gets it back over RPC."""
    try:
        raise exc
    except Exception:
        wire = msg_common.serialize_remote_exception(sys.exc_info())
    return msg_common.deserialize_remote_exception(
        wire, ['zun.common.exception'])


@mock.patch('zun.common.utils.validate_container_state', new=mock.Mock())
class TestRemoteTraceback(api_base.FunctionalTest):

    def setUp(self):
        super(TestRemoteTraceback, self).setUp()
        self.container = utils.create_test_container(context=self.context,
                                                     status='Running')

    def test_the_node_s_traceback_is_not_answered(self):
        remote = _as_received(exception.Conflict(
            'Timeout on executing command: sleep 30 did not finish within '
            '2 seconds'))
        # What makes this test mean anything: the message as received
        # carries the node's traceback.
        self.assertIn('Traceback (most recent call last):', str(remote))

        with mock.patch('zun.compute.rpcapi.API.container_exec',
                        side_effect=remote):
            response = self.post(
                '/v1/containers/%s/execute?command=x&run=true'
                % self.container.uuid,
                headers={'OpenStack-API-Version': 'container 1.58'},
                expect_errors=True)

        self.assertEqual(409, response.status_int)
        error = response.json['errors'][0]
        for field in ('title', 'detail'):
            self.assertNotIn('Traceback', error[field])
            self.assertNotIn('.py', error[field])
        self.assertEqual('Timeout on executing command: sleep 30 did not '
                         'finish within 2 seconds', error['detail'])
