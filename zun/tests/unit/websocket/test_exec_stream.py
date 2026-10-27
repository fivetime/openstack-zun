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

"""API 1.59: a streamed exec's token, frames and handshake."""

import json
import struct
import time
from unittest import mock

from websockify import websockifyserver

from zun.common import exception
from zun.tests import base
from zun.websocket import channel
from zun.websocket import exec_stream
from zun.websocket import exec_token
from zun.websocket import websocketproxy


class TestExecToken(base.TestCase):

    def setUp(self):
        super(TestExecToken, self).setUp()
        self.config(exec_token_key='s3cret', group='websocket_proxy')

    def test_what_is_sealed_is_what_is_opened(self):
        payload = {'uuid': 'u', 'target': {'kind': 'cri',
                                           'url': 'http://127.0.0.1:1/x'}}
        token = exec_token.seal(payload)

        self.assertEqual(payload, exec_token.open_token(token))
        # Sealed, not only signed: the runtime's address is not readable.
        self.assertNotIn('127.0.0.1', token)

    def test_another_secret_opens_nothing(self):
        token = exec_token.seal({'uuid': 'u'})
        self.config(exec_token_key='other', group='websocket_proxy')

        self.assertRaises(exception.InvalidWebsocketToken,
                          exec_token.open_token, token)

    def test_an_old_token_opens_nothing(self):
        token = exec_token.seal({'uuid': 'u'})
        self.config(exec_token_ttl=1, group='websocket_proxy')

        with mock.patch('time.time', return_value=time.time() + 5):
            self.assertRaises(exception.InvalidWebsocketToken,
                              exec_token.open_token, token)

    def test_garbage_opens_nothing(self):
        for token in ('', 'garbage', '中'):
            self.assertRaises(exception.InvalidWebsocketToken,
                              exec_token.open_token, token)

    def test_without_a_secret_streaming_is_refused(self):
        self.config(exec_token_key=None, group='websocket_proxy')

        self.assertRaises(exception.Conflict, exec_token.ensure_enabled)


class TestChannel(base.TestCase):

    def _status(self, frame):
        self.assertEqual(channel.ERROR, frame[0])
        return json.loads(frame[1:])

    def test_an_exit_code_is_said_the_way_kubelet_says_it(self):
        self.assertEqual('Success',
                         self._status(channel.exit_code(0))['status'])
        status = self._status(channel.exit_code(3))
        self.assertEqual('NonZeroExitCode', status['reason'])
        self.assertEqual([{'reason': 'ExitCode', 'message': '3'}],
                         status['details']['causes'])

    def test_a_timeout_has_a_reason_of_its_own(self):
        status = self._status(channel.timeout(5))
        self.assertEqual(('Failure', 'Timeout'),
                         (status['status'], status['reason']))


def _docker(stream, data):
    return struct.pack('>BxxxL', stream, len(data)) + data


class TestDockerFrames(base.TestCase):

    def test_frames_are_passed_on_as_they_arrive(self):
        wire = _docker(1, b'hello') + _docker(2, b'oops') + \
            _docker(1, b'x' * 10)
        frames = exec_stream._DockerFrames()
        got = []
        # One byte at a time: headers and payloads split anywhere.
        for i in range(len(wire)):
            got.extend(frames.feed(wire[i:i + 1]))

        out = b''.join(f[1:] for f in got if f[0] == channel.STDOUT)
        err = b''.join(f[1:] for f in got if f[0] == channel.STDERR)
        self.assertEqual(b'hello' + b'x' * 10, out)
        self.assertEqual(b'oops', err)

    def test_a_large_frame_is_not_held_back(self):
        frames = exec_stream._DockerFrames()
        got = frames.feed(_docker(1, b'z' * 100)[:50])

        self.assertEqual(channel.frame(channel.STDOUT, b'z' * 42), got[0])


class _SlowClient(object):
    """websockify's queue for a client slower than the command.

    New frames find the socket full and wait; a later call, made when the
    socket is writable again, sends one.
    """

    def __init__(self):
        self.send_parts = []
        self.sent = []
        self.request = mock.Mock()

    def send_frames(self, bufs=None):
        if bufs:
            self.send_parts.extend(bufs)
        elif self.send_parts:
            self.sent.append(self.send_parts.pop(0))
        return bool(self.send_parts)


class TestTheEnd(base.TestCase):

    @mock.patch('select.select')
    def test_what_is_queued_reaches_the_client_before_it_closes(self, sel):
        """Lost before: 160 KiB from 32 MiB, and the Status after it."""
        client = _SlowClient()
        stream = exec_stream.ExecStream({'target': {}})
        stream.close = mock.Mock()

        def relay():
            for i in range(5):
                stream._to_client([b'\x01%d' % i])
            stream._end_with(channel.exit_code(0))
        stream.relay = relay

        stream.run(client)

        self.assertEqual(6, len(client.sent))
        self.assertEqual(channel.ERROR, client.sent[-1][0])
        stream.close.assert_called_once_with()

    @mock.patch.object(exec_stream, 'DRAIN_TIMEOUT', 0)
    def test_a_client_that_never_reads_is_not_waited_for_forever(self):
        client = _SlowClient()
        client.send_frames = lambda bufs=None: True
        client.send_parts = [b'x']
        stream = exec_stream.ExecStream({'target': {}})
        stream.handler = client

        stream._drain()


class TestCriStream(base.TestCase):

    def _stream(self, protocol, stdin=True):
        stream = exec_stream.CriExecStream(
            {'target': {'kind': 'cri', 'url': 'http://x'}, 'stdin': stdin})
        stream.protocol = protocol
        stream.handler = mock.Mock(send_parts=[])
        stream.backend = mock.Mock()
        stream._stdin = mock.Mock()
        return stream

    def test_closing_stdin_on_a_v4_runtime_fails_at_once(self):
        """Passed on, a v4 runtime ignores it and the command never ends."""
        stream = self._stream(channel.V4)

        self.assertFalse(stream._from_client(bytes((channel.CLOSE, 0))))

        sent = stream.handler.send_frames.call_args.args[0][0]
        self.assertEqual('BadRequest', json.loads(sent[1:])['reason'])
        stream._stdin.put.assert_not_called()

    def test_closing_stdin_on_a_v5_runtime_is_passed_on(self):
        stream = self._stream(channel.V5)

        self.assertTrue(stream._from_client(bytes((channel.CLOSE, 0))))

        stream._stdin.put.assert_called_once()

    def test_no_stdin_when_none_was_asked_for(self):
        stream = self._stream(channel.V5, stdin=False)

        stream._from_client(bytes((channel.STDIN,)) + b'x')

        stream._stdin.put.assert_not_called()

    @mock.patch('websocket.create_connection')
    def test_a_runtime_that_names_no_subprotocol_is_v4(self, connect):
        connect.return_value = mock.Mock(subprotocol=None)
        stream = exec_stream.CriExecStream(
            {'target': {'kind': 'cri', 'url': 'http://127.0.0.1:9/exec/t'}})
        stream.connect()

        self.assertEqual(channel.V4, stream.protocol)
        self.assertEqual('ws://127.0.0.1:9/exec/t', connect.call_args.args[0])
        self.assertEqual([channel.V5, channel.V4],
                         connect.call_args.kwargs['subprotocols'])


class TestDockerStream(base.TestCase):

    @mock.patch('zun.container.docker.utils.DockerHTTPClient')
    def test_an_exec_started_once_is_not_started_again(self, client):
        client.return_value.exec_inspect.return_value = {'Running': False,
                                                         'ExitCode': 0}
        stream = exec_stream.DockerExecStream(
            {'target': {'kind': 'docker', 'url': 'https://n:2376',
                        'exec_id': 'e'}})

        e = self.assertRaises(exec_stream.StreamRefused, stream.connect)
        self.assertEqual(403, e.code)

    def test_a_failed_connection_is_not_a_success(self):
        """Lost output said to be whole is what this must never do."""
        stream = exec_stream.DockerExecStream(
            {'host': 'node1', 'target': {'kind': 'docker', 'url': 'x',
                                         'exec_id': 'e'}})
        stream.exec_id = 'e'
        stream.sock = mock.Mock()
        stream.sock.recv.side_effect = ConnectionResetError()

        self.assertRaises(exec_stream.StreamBroken, stream._read)

    @mock.patch('zun.compute.rpcapi.API')
    def test_the_node_is_asked_to_kill_it(self, rpc):
        stream = exec_stream.DockerExecStream(
            {'host': 'node1', 'target': {'kind': 'docker', 'url': 'x',
                                         'exec_id': 'e'}})
        stream.exec_id = 'e'

        stream.end_command()

        rpc.return_value.container_exec_kill.assert_called_once_with(
            mock.ANY, 'node1', 'e')


class TestHandshake(base.TestCase):
    """The runtime is dialled before the upgrade is answered."""

    def setUp(self):
        super(TestHandshake, self).setUp()
        self.config(exec_token_key='s3cret', group='websocket_proxy')
        self.handler = mock.Mock(headers={'Host': 'proxy'})
        self.handler.wfile = mock.Mock()

    def _validate(self, uuid, payload):
        token = exec_token.seal(payload)
        self.handler.path = '/?stream=exec&uuid=%s&token=%s' % (uuid, token)
        return websocketproxy.ZunProxyRequestHandlerBase.validate_connection(
            self.handler)

    @mock.patch.object(exec_stream, 'open_stream')
    def test_a_token_for_another_container_is_refused(self, open_stream):
        self.assertRaises(websockifyserver.WebSockifyServer.EClose,
                          self._validate, 'other', {'uuid': 'u'})

        self.handler.send_response.assert_called_once_with(403)
        open_stream.assert_not_called()

    @mock.patch.object(exec_stream, 'open_stream')
    def test_a_runtime_that_refuses_is_a_502_with_a_reason(self, open_stream):
        open_stream.side_effect = exec_stream.StreamRefused(502, 'gone')

        self.assertRaises(websockifyserver.WebSockifyServer.EClose,
                          self._validate, 'u', {'uuid': 'u'})

        self.handler.send_response.assert_called_once_with(502)
        self.assertEqual(b'gone\n',
                         self.handler.wfile.write.call_args.args[0])

    @mock.patch.object(exec_stream, 'open_stream')
    def test_the_stream_is_ready_before_the_upgrade(self, open_stream):
        self._validate('u', {'uuid': 'u'})

        self.assertIs(open_stream.return_value, self.handler.exec_stream)

    @mock.patch.object(exec_stream, 'open_stream')
    def test_under_eventlet_the_runtime_is_dialled_on_its_own_hub(
            self, open_stream):
        order = []
        open_stream.side_effect = lambda p: order.append('dial')
        with mock.patch.object(websocketproxy.monkey_patch,
                               'concurrency_mode_threading',
                               return_value=False), \
                mock.patch('eventlet.hubs.use_hub',
                           side_effect=lambda: order.append('hub')):
            self._validate('u', {'uuid': 'u'})

        self.assertEqual(['hub', 'dial'], order)

    def test_the_connection_ends_with_a_close_frame(self):
        handler = mock.Mock(exec_stream=mock.Mock(spec=exec_stream.ExecStream))
        handler.CClose = websockifyserver.WebSockifyRequestHandler.CClose

        e = self.assertRaises(
            handler.CClose,
            websocketproxy.ZunProxyRequestHandlerBase.new_websocket_client,
            handler)

        handler.exec_stream.run.assert_called_once_with(handler)
        self.assertEqual(1000, e.args[0])

    def test_a_v4_client_is_served_by_a_v5_runtime(self):
        """What qinling asks for: v4 framing, no stdin to close."""
        handler = mock.Mock(exec_stream=mock.Mock(protocol=channel.V5))
        ws = websocketproxy.ExecStreamWebSocket(handler)

        self.assertEqual('v4.channel.k8s.io',
                         ws.select_subprotocol(['v4.channel.k8s.io']))
        self.assertEqual('v5.channel.k8s.io', ws.select_subprotocol(
            ['v4.channel.k8s.io', 'v5.channel.k8s.io']))

    def test_the_answer_says_what_was_negotiated(self):
        handler = mock.Mock(exec_stream=mock.Mock(protocol=channel.V4))
        ws = websocketproxy.ExecStreamWebSocket(handler)

        self.assertEqual(' v4.channel.k8s.io', ws.select_subprotocol(
            ['v5.channel.k8s.io', ' v4.channel.k8s.io']))
        self.assertEqual('binary', ws.select_subprotocol(['binary']))
        # Nothing newer than the runtime speaks is chosen.
        self.assertEqual('', ws.select_subprotocol(['v5.channel.k8s.io']))
        ws.end_headers()
        handler.send_header.assert_called_once_with('X-Zun-Exec-Protocol',
                                                    channel.V4)
