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

"""Kubernetes' channel protocol, as a streamed exec (API 1.59) speaks it.

One websocket message is one frame: a byte naming the channel, then data.
It is what the runtime's own streaming server speaks, what kubectl and
client-go speak, and so what a caller already has a client for. v5 differs
from v4 only by the close frame, [255, channel], which is how a caller says
it has no more stdin -- without it, `cat` waits forever.

The command's end is a Status on the error channel, the way kubelet ends
one: Success, or Failure with a reason. Timeout is this API's own; nothing
in Kubernetes ends an exec for running too long.
"""

from oslo_serialization import jsonutils

V4 = 'v4.channel.k8s.io'
V5 = 'v5.channel.k8s.io'
# Newest first: what is offered to a runtime, and what a client asking for
# the channel protocol by name is answered with.
PROTOCOLS = (V5, V4)

STDIN = 0
STDOUT = 1
STDERR = 2
ERROR = 3
RESIZE = 4
CLOSE = 255


def frame(channel, data=b''):
    return bytes((channel,)) + data


def _status(status, **fields):
    body = {'metadata': {}, 'status': status}
    body.update(fields)
    return frame(ERROR, jsonutils.dump_as_bytes(body))


def success():
    return _status('Success')


def exit_code(code):
    if code == 0:
        return success()
    return _status(
        'Failure',
        message='command terminated with non-zero exit code: %d' % code,
        reason='NonZeroExitCode',
        details={'causes': [{'reason': 'ExitCode', 'message': str(code)}]})


def timeout(seconds):
    return _status(
        'Failure',
        message='command did not finish within %d seconds and was killed'
                % seconds,
        reason='Timeout')


def failure(message, reason='InternalError'):
    return _status('Failure', message=message, reason=reason)


def is_status(message):
    return bool(message) and message[0] == ERROR
