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

import socket
import struct
import threading
import time
from unittest import mock

from oslo_serialization import jsonutils

from zun.container.docker import utils as docker_utils
from zun.tests.unit.container import base


class TestDockerHTTPClient(base.DriverTestCase):

    def setUp(self):
        super(TestDockerHTTPClient, self).setUp()
        self.client = docker_utils.DockerHTTPClient()

    @mock.patch('tarfile.open')
    def test_read_tar_image(self, mock_open):
        fake_image = {'path': 'fake-path'}
        mock_context_manager = mock.MagicMock()
        mock_open.return_value = mock_context_manager
        mock_file = mock.MagicMock()
        mock_context_manager.__enter__.return_value = mock_file
        mock_data = [{"Config": "fake_config",
                      "RepoTags": ["cirros:latest"],
                      "Layers": ["fake_layer", "fake_layer2"]}]
        mock_file.extractfile.return_value.read.return_value = \
            jsonutils.dumps(mock_data, separators=(',', ':'))

        self.client.read_tar_image(fake_image)
        self.assertEqual('cirros', fake_image['repo'])
        self.assertEqual('latest', fake_image['tag'])

    @mock.patch('tarfile.open')
    def test_read_tar_image_multi_tags(self, mock_open):
        fake_image = {'path': 'fake-path'}
        mock_context_manager = mock.MagicMock()
        mock_open.return_value = mock_context_manager
        mock_file = mock.MagicMock()
        mock_context_manager.__enter__.return_value = mock_file
        mock_data = [{"Config": "fake_config",
                      "RepoTags": ["cirros:latest", "cirros:0.3.4"],
                      "Layers": ["fake_layer", "fake_layer2"]}]
        mock_file.extractfile.return_value.read.return_value = \
            jsonutils.dumps(mock_data, separators=(',', ':'))

        self.client.read_tar_image(fake_image)
        self.assertEqual('cirros', fake_image['repo'])
        self.assertEqual('latest', fake_image['tag'])

    @mock.patch('tarfile.open')
    def test_read_tar_image_no_repotag(self, mock_open):
        fake_image = {'path': 'fake-path'}
        mock_context_manager = mock.MagicMock()
        mock_open.return_value = mock_context_manager
        mock_file = mock.MagicMock()
        mock_context_manager.__enter__.return_value = mock_file
        mock_data = [{"Config": "fake_config",
                      "RepoTags": "",
                      "Layers": ["fake_layer", "fake_layer2"]}]
        mock_file.extractfile.return_value.read.return_value = \
            jsonutils.dumps(mock_data, separators=(',', ':'))

        self.client.read_tar_image(fake_image)
        self.assertEqual('fake_config', fake_image['repo'])
        self.assertEqual('', fake_image['tag'])


def _frame(stream_id, payload):
    return struct.pack('>BxxxL', stream_id, len(payload)) + payload


class TestReadExecOutput(base.DriverTestCase):

    def setUp(self):
        super().setUp()
        self.ours, self.docker_end = socket.socketpair()
        # A reader that gives up early must not leave the writer blocked
        # on a full socket, which the cleanup would then wait on forever.
        self.docker_end.settimeout(5)
        self.addCleanup(self.docker_end.close)
        self.addCleanup(self.ours.close)

    def _write_later(self, *chunks, close=True, delay=0.05):
        def writer():
            try:
                for chunk in chunks:
                    time.sleep(delay)
                    self.docker_end.sendall(chunk)
                if close:
                    self.docker_end.shutdown(socket.SHUT_WR)
            except OSError:
                pass    # the reader already hung up, as some tests want
        t = threading.Thread(target=writer)
        t.start()
        self.addCleanup(t.join)

    def test_joins_stdout_and_stderr_in_order(self):
        self._write_later(_frame(1, b'out1 '), _frame(2, b'err '),
                          _frame(1, b'out2'))
        self.assertEqual(b'out1 err out2',
                         docker_utils.read_exec_output(self.ours, 5).combined)

    def test_frames_split_across_reads(self):
        stream = _frame(1, b'hello ') + _frame(2, b'world')
        self._write_later(*[stream[i:i + 3]
                            for i in range(0, len(stream), 3)],
                          delay=0.01)
        self.assertEqual(b'hello world',
                         docker_utils.read_exec_output(self.ours, 5).combined)

    def test_no_output(self):
        self._write_later()
        self.assertEqual(
            b'', docker_utils.read_exec_output(self.ours, 5).combined)

    def test_gives_up_on_a_quiet_command(self):
        # Writes once and then neither writes nor closes: what docker-py
        # would wait on forever.
        self._write_later(_frame(1, b'started'), close=False)
        start = time.monotonic()
        self.assertRaises(socket.timeout,
                          docker_utils.read_exec_output, self.ours, 0.5)
        self.assertLess(time.monotonic() - start, 2)

    def test_deadline_is_for_the_whole_read(self):
        # Each chunk arrives well inside the timeout, all of them together
        # do not: a per-recv timeout alone would never fire.
        self._write_later(*[_frame(1, b'.')] * 20, close=False, delay=0.1)
        start = time.monotonic()
        self.assertRaises(socket.timeout,
                          docker_utils.read_exec_output, self.ours, 0.5)
        self.assertLess(time.monotonic() - start, 1)

    def test_hangs_up_when_done(self):
        self._write_later(close=False)
        self.assertRaises(socket.timeout,
                          docker_utils.read_exec_output, self.ours, 0.2)
        # Docker's end sees the connection go away.
        self.docker_end.settimeout(2)
        self.assertEqual(b'', self.docker_end.recv(1))

    def test_unwraps_socketio(self):
        sio = socket.SocketIO(self.ours, 'rb')
        self._write_later(_frame(1, b'via SocketIO'))
        self.assertEqual(b'via SocketIO',
                         docker_utils.read_exec_output(sio, 5).combined)

    def test_streams_apart(self):
        self._write_later(_frame(1, b'out1 '), _frame(2, b'err'),
                          _frame(1, b'out2'))
        output = docker_utils.read_exec_output(self.ours, 5)
        self.assertEqual(b'out1 out2', output.stdout)
        self.assertEqual(b'err', output.stderr)
        self.assertEqual(set(), output.truncated)

    def test_reads_to_the_end_and_keeps_the_limit(self):
        # Far more than the limit, in frames docker would send: the
        # command is read to its end (the writer closes, so the read
        # returns rather than timing out) and only the limit is kept.
        big = b'x' * 4096
        self._write_later(*[_frame(1, big)] * 64, _frame(2, b'tail'),
                          delay=0)
        output = docker_utils.read_exec_output(self.ours, 5, limit=1024)
        self.assertEqual(b'x' * 1024, output.stdout)
        self.assertEqual(b'tail', output.stderr)
        self.assertEqual({'stdout'}, output.truncated)

    def test_one_huge_frame_is_not_held_whole(self):
        # docker's frame header carries a 32-bit length. A payload is fed
        # as it arrives, not gathered into one frame first.
        payload = b'y' * (1024 * 1024)
        seen = []
        real_feed = docker_utils.exec_output.ExecOutput.feed

        def feed(output, stream, data):
            seen.append(len(data))
            return real_feed(output, stream, data)
        with mock.patch.object(docker_utils.exec_output.ExecOutput, 'feed',
                               feed):
            self._write_later(_frame(1, payload), delay=0)
            output = docker_utils.read_exec_output(self.ours, 5, limit=1024)
        self.assertEqual(b'y' * 1024, output.stdout)
        self.assertLessEqual(max(seen), 65536)

    def test_header_split_across_reads(self):
        stream = _frame(2, b'ab') + _frame(1, b'cd')
        self._write_later(*[stream[i:i + 1] for i in range(len(stream))],
                          delay=0.001)
        output = docker_utils.read_exec_output(self.ours, 5)
        self.assertEqual(b'cd', output.stdout)
        self.assertEqual(b'ab', output.stderr)
        self.assertEqual(b'abcd', output.combined)
