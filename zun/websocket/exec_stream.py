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

"""A streamed exec (API 1.59), as zun-wsproxy serves it.

The client speaks the channel protocol (see channel) whatever the runtime
is. Two runtimes, two ways in:

* The CRI runtime's streaming server already speaks it. The proxy dials the
  url the runtime made, offering v5 first, and passes frames through; the
  runtime ends the command itself when the connection does (a containerd
  that does not, kills nothing -- the fork's does).
* A docker daemon speaks its own: exec start is an HTTP upgrade carrying
  stdout and stderr in 8-byte-headed frames, and stdin raw the other way.
  The proxy starts the exec and translates. Docker ends nothing when its
  connection does, so the proxy has the node kill it (container_exec_kill).

Nothing is buffered beyond what is in flight: output is read from the
runtime only while what the client has not taken is under
[websocket_proxy] stream_buffer_limit, so a slow reader slows the command
instead of growing this process.
"""

import errno
import os
import queue
import select
import socket
import ssl
import struct
import threading
import time

from oslo_log import log as logging
import websocket

from zun.common import context as zun_context
from zun.common import exception
from zun.compute import rpcapi
import zun.conf
from zun.container.docker import utils as docker_utils
from zun.websocket import channel

CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)

# How long to wait for the daemon to say a finished exec is finished: the
# stream ends a moment before the exec's state does.
EXIT_CODE_WAIT = 5
# How long a command asked to be killed at its deadline may take to end
# before its client is told anyway.
KILL_GRACE = 10
# How long a client that has stopped reading is waited for, at the end,
# to take what is still queued for it.
DRAIN_TIMEOUT = 30
# Frames of stdin waiting for a runtime that is not reading them yet.
STDIN_QUEUE = 64


class StreamBroken(Exception):
    """The runtime's connection failed mid-stream."""


class StreamRefused(Exception):
    """The exec cannot be reached; said to the client before the upgrade.

    `reason` is what the client reads, and never names an address: the
    runtime's is in the token precisely so that callers do not see it.
    """

    def __init__(self, code, reason):
        super(StreamRefused, self).__init__(reason)
        self.code = code
        self.reason = reason


def open_stream(payload):
    """The stream a token's payload names, ready for its client."""
    target = payload['target']
    kind = target.get('kind')
    if kind == 'cri':
        stream = CriExecStream(payload)
    elif kind == 'docker':
        stream = DockerExecStream(payload)
    else:
        raise StreamRefused(400, 'unknown exec target')
    stream.connect()
    return stream


class ExecStream(object):
    """What both ways in share: the deadline, the end, the client side."""

    # What the runtime lets the client do: v5 when stdin can be closed.
    protocol = channel.V5

    def __init__(self, payload):
        self.payload = payload
        self.target = payload['target']
        self.stdin = payload.get('stdin', False)
        self.timeout = payload.get('timeout')
        self.timed_out = False
        self._watchdog = None

    # The deadline is kept by its own thread because the main loop can be
    # held up in a send to a client that stopped reading; what it does is
    # end the command, and the main loop then finds the stream ended.
    def _start_deadline(self):
        if not self.timeout:
            return
        self._watchdog = threading.Timer(self.timeout, self._expire)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _expire(self):
        self.expired_at = time.time()
        self.timed_out = True
        LOG.info('Streamed exec ran past its %ss timeout, ending it',
                 self.timeout)
        self.end_command()

    def _stop_deadline(self):
        if self._watchdog:
            self._watchdog.cancel()

    def run(self, handler):
        self.handler = handler
        self._last_sent = time.time()
        self._start_deadline()
        try:
            self.relay()
            self._drain()
        finally:
            self._stop_deadline()
            self.close()

    def _drain(self):
        """Send the client what is still queued for it, the end included.

        The client's socket does not block: what it has not taken waits in
        websockify's queue, and the connection ending first loses it --
        output from the middle, and the Status that says how it ended.
        """
        deadline = time.time() + DRAIN_TIMEOUT
        while self.handler.send_parts:
            left = deadline - time.time()
            if left <= 0:
                LOG.info('Gave up on a streamed exec client that stopped '
                         'reading its last %d bytes', self._pending(
                             self.handler))
                return
            select.select([], [self.handler.request], [], min(left, 1))
            self.handler.send_frames()

    @staticmethod
    def _pending(handler):
        return sum(len(p) for p in handler.send_parts)

    def _to_client(self, frames):
        self._last_sent = time.time()
        self.handler.send_frames(frames)

    def _keep_alive(self):
        """Ping a client that has been sent nothing for a heartbeat.

        A command can be silent for longer than whatever stands between the
        proxy and its client lets a stream idle -- Envoy's default is five
        minutes -- and a stream cut there loses the output that comes after
        and the Status. Any frame resets such a timer; a ping is the one
        every websocket client answers and none passes on as output.
        """
        interval = CONF.websocket_proxy.heartbeat
        if (not interval or self.handler.send_parts or
                time.time() - self._last_sent < interval):
            return
        # Only into a socket with room for it: a ping cut short would leave
        # websockify a partial frame to finish before any other.
        if not select.select([], [self.handler.request], [], 0)[1]:
            return
        try:
            self.handler.send_ping()
        except ssl.SSLWantWriteError:
            return
        self._last_sent = time.time()

    def _end_with(self, status):
        self._to_client([status])

    def end_command(self):
        raise NotImplementedError()

    def connect(self):
        raise NotImplementedError()

    def relay(self):
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()


class CriExecStream(ExecStream):
    """The runtime's own stream, passed through frame for frame."""

    def connect(self):
        url = self.target['url']
        if url.startswith('https'):
            url = 'wss' + url[len('https'):]
        elif url.startswith('http'):
            url = 'ws' + url[len('http'):]
        try:
            self.backend = websocket.create_connection(
                url, subprotocols=list(channel.PROTOCOLS),
                skip_utf8_validation=True, enable_multithread=True)
        except websocket.WebSocketBadStatusException as e:
            # Used, expired, or never made: the runtime's urls are single
            # use and short lived.
            LOG.info('The runtime refused a streamed exec: %s', e)
            raise StreamRefused(502, 'the container runtime refused this '
                                     'exec; it may have been used already')
        except (OSError, websocket.WebSocketException) as e:
            LOG.warning('Could not reach the runtime for a streamed exec: '
                        '%s', e)
            raise StreamRefused(502, 'the container runtime could not be '
                                     'reached')
        # A runtime that predates v5 answers v4, or nothing at all, which is
        # v4 too: the subprotocol it serves exec on before any had a name.
        self.protocol = self.backend.subprotocol or channel.V4
        self._stdin = queue.Queue(STDIN_QUEUE)
        self._writer = None

    def end_command(self):
        # The runtime ends the command when its stream ends.
        try:
            self.backend.abort()
        except OSError:
            pass

    def _write_stdin(self):
        while True:
            message = self._stdin.get()
            if message is None:
                return
            try:
                self.backend.send_binary(message)
            except (OSError, websocket.WebSocketException):
                return

    def relay(self):
        # Written from a thread of its own: a command that does not read
        # its stdin must not stop its output from being read.
        self._writer = threading.Thread(target=self._write_stdin,
                                        daemon=True)
        self._writer.start()
        backend = self.backend.sock
        status_seen = False
        while True:
            rlist = [self.handler.request]
            if self._pending(self.handler) < CONF.websocket_proxy.\
                    stream_buffer_limit:
                rlist.append(backend)
            wlist = [self.handler.request] if self.handler.send_parts else []
            try:
                ins, outs, _x = select.select(rlist, wlist, [], 1)
            except (OSError, ValueError):
                # The deadline closed the runtime's socket under us.
                ins, outs = [], []
                if not self.timed_out:
                    raise
            if outs:
                self.handler.send_frames()
            self._keep_alive()
            if self.timed_out:
                self._end_with(channel.timeout(self.timeout))
                return
            if self.handler.request in ins:
                messages, closed = self.handler.recv_frames()
                for message in messages:
                    if not self._from_client(message):
                        return
                if closed:
                    LOG.debug('Client left a streamed exec')
                    self.end_command()
                    return
            if backend in ins:
                try:
                    opcode, data = self.backend.recv_data()
                except (OSError, websocket.WebSocketException):
                    opcode, data = websocket.ABNF.OPCODE_CLOSE, b''
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    if self.timed_out:
                        self._end_with(channel.timeout(self.timeout))
                    elif not status_seen:
                        self._end_with(channel.failure(
                            'the container runtime ended the stream '
                            'without saying how the command ended'))
                    return
                if isinstance(data, str):
                    data = data.encode()
                if channel.is_status(data):
                    status_seen = True
                self._to_client([data])

    def _from_client(self, message):
        if not message:
            return True
        if message[0] == channel.CLOSE and self.protocol != channel.V5:
            # The runtime has no way to be told; passed on it is ignored,
            # and the command waits for an end of input that never comes.
            self._end_with(channel.failure(
                'this node\'s container runtime cannot close stdin '
                '(it speaks %s)' % self.protocol, reason='BadRequest'))
            return False
        if message[0] in (channel.STDIN, channel.CLOSE) and not self.stdin:
            return True
        try:
            self._stdin.put(message, timeout=1)
        except queue.Full:
            # Held until the runtime takes it; the client waits meanwhile.
            self._stdin.put(message)
        return True

    def close(self):
        if self._writer:
            try:
                self._stdin.put_nowait(None)
            except queue.Full:
                pass
        try:
            self.backend.close(timeout=1)
        except Exception:
            pass


class _DockerFrames(object):
    """Docker's multiplexed exec stream, read as it arrives.

    Each frame is a stream byte, three zero bytes and a big-endian length;
    the payload is passed on as soon as any of it is here rather than
    once all of it is, which is what makes this a stream.
    """

    CHANNELS = {1: channel.STDOUT, 2: channel.STDERR}

    def __init__(self):
        self._header = b''
        self._stream = None
        self._left = 0

    def feed(self, data):
        frames = []
        view = memoryview(data)
        while view:
            if self._left == 0:
                need = 8 - len(self._header)
                self._header += bytes(view[:need])
                view = view[need:]
                if len(self._header) < 8:
                    break
                stream, length = struct.unpack('>BxxxL', self._header)
                self._header = b''
                self._stream = self.CHANNELS.get(stream, channel.STDOUT)
                self._left = length
                continue
            chunk = bytes(view[:self._left])
            view = view[len(chunk):]
            self._left -= len(chunk)
            frames.append(channel.frame(self._stream, chunk))
        return frames


class DockerExecStream(ExecStream):
    """An exec on a docker daemon, translated to the channel protocol."""

    def connect(self):
        url = self.target['url']
        kwargs = {}
        if url.startswith('https'):
            kwargs = {'ca_cert': CONF.docker.ca_file,
                      'client_key': CONF.docker.key_file,
                      'client_cert': CONF.docker.cert_file}
        self.exec_id = self.target['exec_id']
        try:
            self.docker = docker_utils.DockerHTTPClient(
                url, CONF.docker.docker_remote_api_version,
                CONF.docker.default_timeout, **kwargs)
            info = self.docker.exec_inspect(self.exec_id)
        except Exception as e:
            LOG.warning('Could not reach the docker daemon for a streamed '
                        'exec: %s', e)
            raise StreamRefused(502, 'the container runtime could not be '
                                     'reached, or no longer has this exec')
        if info.get('Running') or info.get('ExitCode') is not None:
            # Started once already: docker starts an exec only once.
            raise StreamRefused(403, 'this exec has been used already')
        self.sock = None
        self._started = False

    def _start(self):
        sock, self.head = docker_utils.start_exec(self.docker, self.exec_id)
        self._keep = sock
        # docker-py hands back the TLS socket itself, or a SocketIO around
        # a plain one.
        self.sock = getattr(sock, '_sock', sock)
        self.sock.setblocking(False)
        self._started = True

    def end_command(self):
        # The daemon has no call that stops an exec; the node does, see
        # DockerDriver._kill_exec.
        try:
            ctx = zun_context.get_admin_context(all_projects=True)
            rpcapi.API(context=ctx).container_exec_kill(
                ctx, self.payload['host'], self.exec_id)
        except Exception as e:
            LOG.warning('Could not ask %(host)s to kill streamed exec '
                        '%(id)s: %(err)s',
                        {'host': self.payload['host'], 'id': self.exec_id,
                         'err': e})

    def _exit_code(self):
        deadline = time.time() + EXIT_CODE_WAIT
        while True:
            info = self.docker.exec_inspect(self.exec_id)
            if not info.get('Running') or time.time() >= deadline:
                return info.get('ExitCode')
            time.sleep(0.1)

    def _read(self):
        """What the daemon has sent, b'' at its end, None for nothing yet.

        A connection that fails is not an end: what the command wrote after
        it is lost, and saying Success would hand the client a truncated
        output as a whole one. StreamBroken says it was cut.
        """
        try:
            data = self.sock.recv(65536)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError,
                BlockingIOError):
            return None
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            LOG.warning('Reading streamed exec %(id)s failed: %(err)r',
                        {'id': self.exec_id, 'err': e})
            raise StreamBroken()
        return data

    def _write(self, data):
        """How much of `data` the daemon took; 0 when it took none yet."""
        try:
            return self.sock.send(data)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError,
                BlockingIOError):
            return 0

    def _close_stdin(self):
        # Half closed, as `docker exec -i` does: the daemon closes the
        # command's stdin when its copy of ours ends. Through a dup, because
        # shutting down a TLS socket in Python also drops its TLS state, and
        # the output still has to be read through it.
        raw = socket.socket(fileno=os.dup(self.sock.fileno()))
        try:
            raw.shutdown(socket.SHUT_WR)
        finally:
            raw.close()

    def relay(self):
        try:
            self._start()
        except Exception as e:
            LOG.warning('Could not start streamed exec %(id)s: %(err)s',
                        {'id': self.exec_id, 'err': e})
            self._end_with(channel.failure('the command could not be '
                                           'started'))
            return
        frames = _DockerFrames()
        # The start of the output, read with the upgrade's headers.
        self._to_client(frames.feed(self.head))
        # What the daemon sent, said when it ends: the daemon has been seen
        # to end a stream short of what the command wrote, with the exec's
        # exit code still 0, and this is what tells that apart from a loss
        # anywhere after it.
        self.received = len(self.head)
        self.started_at = time.time()
        stdin = bytearray()
        close_stdin = False
        while True:
            readable_backend = (self._pending(self.handler) <
                                CONF.websocket_proxy.stream_buffer_limit)
            rlist = [self.handler.request]
            if readable_backend:
                rlist.append(self.sock)
            wlist = []
            if stdin or close_stdin:
                wlist.append(self.sock)
            if self.handler.send_parts:
                wlist.append(self.handler.request)
            # TLS may hold a decrypted record select cannot see.
            wait = 0 if (readable_backend and
                         getattr(self.sock, 'pending', lambda: 0)()) else 1
            ins, outs, _x = select.select(rlist, wlist, [], wait)
            if (self.timed_out and
                    time.time() > self.expired_at + KILL_GRACE):
                # Asked to be killed and still running: the client is told
                # now rather than for as long as the command lasts.
                self._end_with(channel.timeout(self.timeout))
                return
            if self.handler.request in outs:
                self.handler.send_frames()
            self._keep_alive()
            if self.handler.request in ins:
                messages, closed = self.handler.recv_frames()
                for message in messages:
                    if not message or not self.stdin:
                        continue
                    if message[0] == channel.STDIN:
                        stdin += message[1:]
                    elif message[0] == channel.CLOSE:
                        close_stdin = True
                if closed:
                    LOG.debug('Client left streamed exec %s', self.exec_id)
                    self.end_command()
                    return
            if stdin and self.sock in outs:
                del stdin[:self._write(bytes(stdin))]
            if close_stdin and not stdin:
                self._close_stdin()
                close_stdin = False
            if readable_backend and (self.sock in ins or wait == 0):
                try:
                    data = self._read()
                except StreamBroken:
                    self.end_command()
                    self._end_with(channel.failure(
                        'the connection to the container runtime failed; '
                        'the output is incomplete'))
                    return
                if data == b'':
                    self._finish()
                    return
                if data:
                    self.received += len(data)
                    self._to_client(frames.feed(data))

    def _finish(self):
        if self.timed_out:
            self._end_with(channel.timeout(self.timeout))
            return
        try:
            code = self._exit_code()
        except Exception as e:
            LOG.warning('Could not read the exit code of streamed exec '
                        '%(id)s: %(err)s', {'id': self.exec_id, 'err': e})
            code = None
        LOG.info('Streamed exec %(id)s ended after %(secs).1f s: '
                 '%(bytes)d bytes from the docker daemon, exit code %(code)s',
                 {'id': self.exec_id, 'secs': time.time() - self.started_at,
                  'bytes': self.received, 'code': code})
        if code is None:
            self._end_with(channel.failure(
                'the command ended but its exit code could not be read'))
        else:
            self._end_with(channel.exit_code(code))

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        try:
            self.docker.close()
        except Exception:
            pass


def refuse(handler, error):
    """Answer the upgrade with an HTTP error the client can read."""
    if isinstance(error, exception.InvalidWebsocketToken):
        code, reason = 403, 'invalid or expired token'
    elif isinstance(error, StreamRefused):
        code, reason = error.code, error.reason
    else:
        code, reason = 400, str(error)
    body = (reason + '\n').encode('utf-8')
    handler.send_response(code)
    handler.send_header('Content-Type', 'text/plain; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.wfile.write(body)
