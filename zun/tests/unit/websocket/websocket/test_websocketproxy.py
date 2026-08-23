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

from unittest import mock

from zun.common import exception
from zun.tests import base
from zun.websocket import websocketproxy


class FakeContainer(object):
    def __init__(self, **kwargs):
        self.uuid = 'a-container'
        self.websocket_url = 'ws://node:2375/v1.44/containers/c/attach/ws'
        self.websocket_token = 'the-attach-token'
        self.logs_url = 'ws://node:2375/v1.44/containers/c/attach/ws?logs=0'
        self.logs_token = 'the-logs-token'
        self.__dict__.update(kwargs)


class TestFollowLogsSession(base.TestCase):
    """The proxy is the only thing that dials a container's stream.

    What it will dial is decided here rather than by the caller: a token it
    minted, against a url the compute node recorded. Nothing a client sends
    names an address.
    """

    def setUp(self):
        super(TestFollowLogsSession, self).setUp()
        # Not spec'd on the handler class: vmsg and the socket plumbing
        # come from websockify's base, which a unit test has no server to
        # give it. What is under test is the method itself.
        self.handler = mock.Mock()

    def _follow(self, container, token):
        return websocketproxy.ZunProxyRequestHandlerBase._new_logs_client(
            self.handler, container, token, container.uuid)

    def test_a_wrong_token_is_refused(self):
        self.assertRaises(exception.InvalidWebsocketToken,
                          self._follow, FakeContainer(), 'not-the-token')

    def test_an_empty_token_is_refused(self):
        """An unset token on the container must not admit an unset one.

        A container nobody has asked to follow has no logs_token; comparing
        two absences would let anyone who omits the parameter through.
        """
        container = FakeContainer(logs_token=None)
        self.assertRaises(exception.InvalidWebsocketToken,
                          self._follow, container, None)

    def test_the_attach_token_does_not_open_the_logs_session(self):
        """Two sessions, two tokens, and neither stands in for the other."""
        container = FakeContainer()
        self.assertRaises(exception.InvalidWebsocketToken,
                          self._follow, container, container.websocket_token)

    @mock.patch.object(websocketproxy, 'WebSocketClient')
    def test_the_recorded_logs_url_is_what_gets_dialled(self, mock_client):
        container = FakeContainer()

        self._follow(container, 'the-logs-token')

        dialled = mock_client.call_args.kwargs['host_url']
        self.assertEqual(container.logs_url, dialled)

    @mock.patch.object(websocketproxy, 'WebSocketClient')
    def test_a_container_with_no_recorded_url_is_refused(self, mock_client):
        container = FakeContainer(logs_url=None)
        self.assertRaises(exception.InvalidWebsocketUrl,
                          self._follow, container, 'the-logs-token')
        mock_client.assert_not_called()

    @mock.patch.object(websocketproxy, 'WebSocketClient')
    def test_the_target_is_closed_when_the_stream_ends(self, mock_client):
        """Whoever stops reading, the connection to the node goes.

        The client is gone the moment the tenant presses ctrl-c, and a
        stream nobody reads still costs the compute node a connection.
        """
        container = FakeContainer()
        self.handler.do_websocket_proxy.side_effect = RuntimeError('gone')

        self.assertRaises(RuntimeError,
                          self._follow, container, 'the-logs-token')

        mock_client.return_value.ws.close.assert_called_once_with()
