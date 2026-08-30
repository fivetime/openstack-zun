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

"""Writing an archive onto an exec's stdin.

Every message carries its channel in the first byte. Saying stdin is
finished is the difference between a tar that unpacks and one cut off
mid-member, and only v5 of the protocol has a way to say it without
closing the connection -- so it is asked for, and its absence is
tolerated rather than assumed.
"""

from unittest import mock

import websocket

from zun.common import exception
from zun.container.cri import stream
from zun.tests import base


class _Socket(object):
    def __init__(self, subprotocol=stream.V5, replies=None):
        self.sent = []
        self._subprotocol = subprotocol
        self._replies = list(replies or [])
        self.closed = False

    def send_binary(self, payload):
        self.sent.append(payload)

    def getsubprotocol(self):
        return self._subprotocol

    def recv(self):
        if not self._replies:
            raise websocket.WebSocketConnectionClosedException()
        return self._replies.pop(0)

    def close(self):
        self.closed = True


class WriteStdinTest(base.TestCase):

    def _write(self, sock, data=b'TAR'):
        with mock.patch.object(stream.websocket, 'create_connection',
                               return_value=sock):
            return stream.write_stdin('wss://node/exec', data, 30)

    def test_the_archive_goes_out_on_the_stdin_channel(self):
        sock = _Socket()
        self._write(sock)

        self.assertEqual(bytes([stream.STDIN]) + b'TAR', sock.sent[0])

    def test_v5_is_told_that_stdin_is_finished(self):
        sock = _Socket(subprotocol=stream.V5)
        self._write(sock)

        self.assertEqual(bytes([stream.CLOSE, stream.STDIN]), sock.sent[-1])

    def test_v4_is_not_sent_a_frame_it_cannot_read(self):
        sock = _Socket(subprotocol=stream.V4)
        self._write(sock)

        self.assertEqual(1, len(sock.sent))

    def test_a_large_archive_is_chunked(self):
        sock = _Socket(subprotocol=stream.V4)
        self._write(sock, data=b'x' * (stream._CHUNK * 2 + 5))

        self.assertEqual(3, len(sock.sent))

    def test_success_says_nothing_and_that_is_the_answer(self):
        sock = _Socket(replies=[bytes([stream.ERROR])
                                + b'{"status":"Success"}'])

        self.assertEqual(b'', self._write(sock))

    def test_a_failure_carries_the_runtime_s_reason(self):
        sock = _Socket(replies=[
            bytes([stream.ERROR])
            + b'{"status":"Failure","message":"tar: /nope: not found"}'])

        error = self.assertRaises(exception.ZunException, self._write, sock)
        self.assertIn('not found', str(error))

    def test_the_connection_is_closed_even_when_it_failed(self):
        sock = _Socket(replies=[bytes([stream.ERROR])
                                + b'{"status":"Failure"}'])
        self.assertRaises(exception.ZunException, self._write, sock)

        self.assertTrue(sock.closed)

    def test_a_stream_that_just_ends_is_not_an_error(self):
        """A command that succeeds says nothing; the stream simply ends."""
        sock = _Socket(replies=[bytes([stream.STDOUT]) + b'hello'])

        self.assertEqual(b'hello', self._write(sock))


class TheUrlIsDialledAsAWebsocketTest(base.TestCase):
    """The runtime hands back http; the client accepts only ws.

    It serves the upgrade on http, so that is what its URL says. A
    client given it refuses with a message about an invalid scheme,
    which reads as a bug in the caller rather than in the URL.
    """

    def test_http_becomes_ws(self):
        self.assertEqual('ws://127.0.0.1:1/exec/x',
                         stream.as_websocket('http://127.0.0.1:1/exec/x'))

    def test_https_becomes_wss(self):
        self.assertEqual('wss://node/exec/x',
                         stream.as_websocket('https://node/exec/x'))

    def test_one_already_right_is_left_alone(self):
        self.assertEqual('wss://node/x', stream.as_websocket('wss://node/x'))

    def test_the_connection_is_made_to_the_converted_url(self):
        with mock.patch.object(stream.websocket, 'create_connection',
                               return_value=_Socket()) as dialled:
            stream.write_stdin('http://127.0.0.1:1/exec/x', b'', 30)

        self.assertEqual('ws://127.0.0.1:1/exec/x',
                         dialled.call_args.args[0])
