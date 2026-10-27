# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""What a command run and waited for wrote, kept within a bound.

The output of a synchronous exec travels whole: from the runtime to the
compute node, over RPC to the API and back to the caller. Nothing bounded
it, so one `cat` of a large file was enough to grow the compute node and
the API worker by the size of that file. The command is not cut short --
it runs to its end and its exit code is real -- but only the first
[compute] exec_output_limit bytes of each stream are kept, and the reply
says which streams lost the rest.

Nothing is stored anywhere: the bytes live as long as the request does.
"""

import base64

import zun.conf

CONF = zun.conf.CONF

STDOUT = 1
STDERR = 2
_NAMES = {STDOUT: 'stdout', STDERR: 'stderr'}


class ExecOutput(object):
    """stdout and stderr of one exec, each kept up to `limit` bytes.

    The order the two were written in is remembered, so the combined
    output older callers read is interleaved as it was before.
    """

    def __init__(self, limit=None):
        self.limit = CONF.compute.exec_output_limit if limit is None \
            else limit
        self._kept = {STDOUT: bytearray(), STDERR: bytearray()}
        # (stream, start, end) runs into _kept, in the order written
        self._runs = []
        self.truncated = set()

    def feed(self, stream, data):
        """Keep what still fits of `data`, written to `stream`.

        Docker's stream 0 is stdin, which a non-tty exec never echoes;
        anything there is counted as stdout rather than dropped.
        """
        if not data:
            return
        stream = STDERR if stream == STDERR else STDOUT
        kept = self._kept[stream]
        room = self.limit - len(kept)
        if len(data) > room:
            self.truncated.add(_NAMES[stream])
            data = data[:max(room, 0)]
        if not data:
            return
        start = len(kept)
        kept += data
        if self._runs and self._runs[-1][0] == stream:
            self._runs[-1] = (stream, self._runs[-1][1], len(kept))
        else:
            self._runs.append((stream, start, len(kept)))

    def mark_truncated(self, stream):
        """Something before us dropped part of `stream` already."""
        self.truncated.add(_NAMES[stream])

    @property
    def stdout(self):
        return bytes(self._kept[STDOUT])

    @property
    def stderr(self):
        return bytes(self._kept[STDERR])

    @property
    def combined(self):
        return b''.join(bytes(self._kept[s][a:b]) for s, a, b in self._runs)

    @classmethod
    def from_combined(cls, output):
        """Wrap what a driver that predates this returned: one blob."""
        result = cls(limit=float('inf'))
        if isinstance(output, str):
            output = output.encode('utf-8')
        result.feed(STDOUT, output or b'')
        return result

    def as_reply(self):
        """The fields of the exec's answer, all of them JSON-safe.

        The answer crosses RPC as JSON, so bytes have to become text here;
        a command writing anything but UTF-8 used to fail the whole request
        with a decode error. A stream that is valid UTF-8 is text; one that
        is not is base64, and says so, so nothing is silently altered.
        `output`, the combined field older callers read, has to stay a
        string: undecodable bytes in it are replaced, and output_replaced
        names the streams that had any.
        """
        reply = {'output_truncated': sorted(self.truncated),
                 'output_replaced': []}
        for stream in (STDOUT, STDERR):
            name = _NAMES[stream]
            try:
                reply[name] = _text(bytes(self._kept[stream]),
                                    name in self.truncated)
                reply[name + '_encoding'] = 'utf-8'
            except UnicodeDecodeError:
                data = bytes(self._kept[stream])
                reply[name] = base64.b64encode(data).decode('ascii')
                reply[name + '_encoding'] = 'base64'
                reply['output_replaced'].append(name)
        reply['output'] = self.combined.decode('utf-8', 'replace')
        return reply


def _text(data, truncated):
    """`data` as text, or UnicodeDecodeError when it is not UTF-8.

    A cut made at the limit can land inside a character. Text that was
    truncated loses that partial character rather than being reported as
    binary.
    """
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as e:
        if (truncated and e.reason == 'unexpected end of data'
                and e.end == len(data)):
            return data[:e.start].decode('utf-8')
        raise
