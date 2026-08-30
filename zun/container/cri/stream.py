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

"""Writing to the stdin of an exec on the runtime's streaming server.

The synchronous exec has no stdin, so anything that has to send bytes
into a container -- `docker cp` writing, for one -- goes through the
same streaming server an interactive session uses. That server speaks
the protocol kubectl does: every message carries the channel it belongs
to in its first byte.

Only the writing half lives here. Reading a session is the websocket
proxy's job and it does it byte for byte; this is for the one caller
that has an archive to push and an answer to wait for.
"""

import json
import socket
import ssl

from oslo_log import log as logging
import websocket

from zun.common import exception
from zun.common.i18n import _

LOG = logging.getLogger(__name__)

#: The first byte of every message says which stream it is.
STDIN, STDOUT, STDERR, ERROR, RESIZE = range(5)
#: v5 added a way to say "stdin is finished" without closing the
#: connection, which is the difference between a tar that unpacks and one
#: that is cut off mid-member. Asked for first; v4 is accepted so that a
#: runtime too old for it still works, with the close standing in.
CLOSE = 255
V5 = 'v5.channel.k8s.io'
V4 = 'v4.channel.k8s.io'

#: Big enough that a copy is not thousands of frames, small enough that
#: one frame is not a memory event on a busy node.
_CHUNK = 256 * 1024


def write_stdin(url, data, timeout):
    """Send `data` to the exec at `url` and report how it ended.

    Returns the runtime's status message, which is empty when the
    command exited zero -- the same shape the protocol's error channel
    uses, where success is the absence of anything to say.
    """
    connection = websocket.create_connection(
        url, timeout=timeout, subprotocols=[V5, V4],
        sslopt={'cert_reqs': ssl.CERT_NONE},
        enable_multithread=True)
    try:
        for start in range(0, len(data), _CHUNK):
            connection.send_binary(
                bytes([STDIN]) + data[start:start + _CHUNK])
        if connection.getsubprotocol() == V5:
            # Say stdin is done and keep listening: the command has not
            # finished merely because it has been fed.
            connection.send_binary(bytes([CLOSE, STDIN]))
        return _read_status(connection)
    finally:
        try:
            connection.close()
        except Exception:                                   # noqa: BLE001
            pass


def _read_status(connection):
    """Read until the runtime says how the command ended, or the stream does.

    A command that fails says so on the error channel; one that succeeds
    says nothing and the stream simply ends. Both are answers, and
    waiting for the wrong one is how this would hang.
    """
    said = []
    while True:
        try:
            frame = connection.recv()
        except (websocket.WebSocketConnectionClosedException,
                websocket.WebSocketTimeoutException, socket.timeout,
                OSError):
            break
        if not frame:
            break
        if isinstance(frame, str):
            frame = frame.encode()
        channel, payload = frame[0], frame[1:]
        if channel == ERROR:
            return _failure(payload)
        if channel in (STDOUT, STDERR) and payload:
            said.append(payload)
    return b''.join(said)


def _failure(payload):
    """The error channel carries a status object, not a sentence."""
    try:
        status = json.loads(payload.decode('utf-8', 'replace'))
    except ValueError:
        status = {}
    if status.get('status') == 'Success':
        return b''
    raise exception.ZunException(_('the command in the container failed: %s')
                                 % (status.get('message')
                                    or payload.decode('utf-8', 'replace')
                                    or 'no reason given'))
