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

"""API 1.58: what a command run and waited for wrote, bounded and JSON-safe.
"""

import base64
from unittest import mock

import grpc
from oslo_messaging._drivers import common as msg_common
from oslo_serialization import jsonutils

from zun.common import exception
from zun.common import rpc
from zun.compute import api as compute_api
from zun.compute import manager
from zun.compute import rpcapi
from zun.conf import compute as compute_conf
from zun.container.cri import driver as cri_driver
from zun.container import exec_output
from zun.objects.container import Container
from zun.tests import base
from zun.tests.unit.container.fake_driver import FakeDriver as fake_driver
from zun.tests.unit.db import utils


class TestExecOutput(base.TestCase):

    def test_each_stream_is_kept_up_to_the_limit(self):
        output = exec_output.ExecOutput(limit=4)
        output.feed(exec_output.STDOUT, b'abcdef')
        output.feed(exec_output.STDERR, b'12')
        output.feed(exec_output.STDERR, b'345')

        self.assertEqual(b'abcd', output.stdout)
        self.assertEqual(b'1234', output.stderr)
        self.assertEqual({'stdout', 'stderr'}, output.truncated)

    def test_combined_is_in_the_order_written(self):
        output = exec_output.ExecOutput(limit=100)
        for stream, data in ((1, b'o1 '), (2, b'e1 '), (1, b'o2 '),
                             (1, b'o3 '), (2, b'e2')):
            output.feed(stream, data)

        self.assertEqual(b'o1 e1 o2 o3 e2', output.combined)

    def test_what_is_dropped_from_one_stream_does_not_cut_the_other(self):
        output = exec_output.ExecOutput(limit=2)
        output.feed(1, b'xxxx')
        output.feed(2, b'ee')

        self.assertEqual(b'xxee', output.combined)
        self.assertEqual({'stdout'}, output.truncated)

    def test_text_is_text(self):
        output = exec_output.ExecOutput(limit=100)
        output.feed(1, '中文 ok'.encode())
        reply = exec_output.client_fields(output.as_wire())

        self.assertEqual('中文 ok', reply['stdout'])
        self.assertEqual('utf-8', reply['stdout_encoding'])
        self.assertEqual('中文 ok', reply['output'])
        self.assertEqual([], reply['output_truncated'])
        self.assertEqual([], reply['output_replaced'])

    def test_a_character_cut_at_the_limit_is_still_text(self):
        output = exec_output.ExecOutput(limit=4)
        output.feed(1, '中文'.encode())    # 6 bytes, cut inside the second
        reply = exec_output.client_fields(output.as_wire())

        self.assertEqual('中', reply['stdout'])
        self.assertEqual('utf-8', reply['stdout_encoding'])
        self.assertEqual(['stdout'], reply['output_truncated'])

    def test_output_and_the_stream_agree_on_a_cut_character(self):
        output = exec_output.ExecOutput(limit=4)
        output.feed(1, '中文'.encode())
        output.feed(2, b'err')
        reply = exec_output.client_fields(output.as_wire())

        self.assertEqual('中err', reply['output'])
        self.assertEqual([], reply['output_replaced'])

    def test_bytes_that_are_not_utf8_come_back_whole_in_base64(self):
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF'
        output = exec_output.ExecOutput(limit=100)
        output.feed(1, jpeg)
        reply = exec_output.client_fields(output.as_wire())

        self.assertEqual('base64', reply['stdout_encoding'])
        self.assertEqual(jpeg, base64.b64decode(reply['stdout']))
        self.assertEqual(['stdout'], reply['output_replaced'])
        self.assertIn('�', reply['output'])

    def test_the_default_limit_is_the_option(self):
        self.config(exec_output_limit=2048, group='compute')

        self.assertEqual(2048, exec_output.ExecOutput().limit)


class TestComputeReply(base.TestCase):
    """From the driver, through RPC as it is serialized, to the API."""

    def setUp(self):
        super(TestComputeReply, self).setUp()
        p = mock.patch('zun.scheduler.client.report.SchedulerReportClient')
        p.start()
        self.addCleanup(p.stop)
        self.config(container_driver='fake', capsule_driver='fake')
        self.compute_manager = manager.Manager()
        self.container = Container(self.context,
                                   **utils.get_test_container())
        self.container.host = 'node1'
        self.serializer = rpc.JsonPayloadSerializer()

    def _on_the_wire(self, driver_output):
        with mock.patch.object(fake_driver, 'execute_create',
                               return_value='e1'), \
                mock.patch.object(fake_driver, 'execute_run',
                                  return_value=(driver_output, 3)):
            reply = self.compute_manager.container_exec(
                self.context, self.container, 'cmd', True, False)
        # What the RPC layer does to a reply before it leaves the node,
        # and what the broker is then handed: the envelope oslo.messaging
        # wraps it in, as JSON.
        payload = self.serializer.serialize_entity(self.context, reply)
        message = jsonutils.dumps(msg_common.serialize_msg(
            {'result': payload, 'failure': None, 'ending': True}))
        return payload, message

    def _answer(self, driver_output):
        payload, _message = self._on_the_wire(driver_output)
        with mock.patch.object(rpcapi.API, 'container_exec',
                               return_value=jsonutils.loads(
                                   jsonutils.dumps(payload))):
            return compute_api.API(self.context).container_exec(
                self.context, self.container, 'cmd', True, False)

    def test_binary_output_no_longer_fails_the_request(self):
        """Before, the serializer raised UnicodeDecodeError: a 500."""
        output = exec_output.ExecOutput(limit=100)
        output.feed(1, b'\xff\xfe binary')
        output.feed(2, b'warning\n')

        answer = self._answer(output)

        self.assertEqual(3, answer['exit_code'])
        self.assertEqual('base64', answer['stdout_encoding'])
        self.assertEqual(b'\xff\xfe binary',
                         base64.b64decode(answer['stdout']))
        self.assertEqual('warning\n', answer['stderr'])
        self.assertEqual(['stdout'], answer['output_replaced'])
        self.assertIsNone(answer['exec_id'])
        self.assertNotIn('stdout_b64', answer)

    def test_a_driver_answering_one_blob_still_works(self):
        answer = self._answer(b'all of it')

        self.assertEqual('all of it', answer['output'])
        self.assertEqual('all of it', answer['stdout'])
        self.assertEqual('', answer['stderr'])
        self.assertEqual([], answer['output_truncated'])

    def test_a_node_that_predates_it_is_passed_on(self):
        """Upgrading the API first: an older node answers `output` alone."""
        old_reply = {'output': 'from an old node', 'exit_code': 0,
                     'exec_id': None, 'token': None}
        with mock.patch.object(rpcapi.API, 'container_exec',
                               return_value=dict(old_reply)):
            answer = compute_api.API(self.context).container_exec(
                self.context, self.container, 'cmd', True, False)

        self.assertEqual('from an old node', answer['output'])
        self.assertEqual(0, answer['exit_code'])
        self.assertNotIn('stdout', answer)

    def test_the_largest_reply_fits_the_broker_s_message(self):
        """The largest reply fits in one RabbitMQ message.

        The output kept at the highest limit allowed, written the way that
        costs most as JSON text (NUL is \\u0000, twice escaped), against
        the broker's max_message_size (16 MiB by default).
        """
        opt = [o for o in compute_conf.ALL_OPTS
               if o.name == 'exec_output_limit'][0]
        output = exec_output.ExecOutput(limit=opt.type.max)
        output.feed(1, b'\x00' * (opt.type.max + 1))
        output.feed(2, '中'.encode() * opt.type.max)

        _payload, message = self._on_the_wire(output)

        self.assertLess(len(message), 16 * 1024 * 1024)

    def test_a_text_reply_used_to_be_seven_times_its_size(self):
        """Why the wire is base64: the same output as text."""
        nul = b'\x00' * 1000
        as_text = jsonutils.dumps(msg_common.serialize_msg(
            {'result': self.serializer.serialize_entity(
                self.context, {'stdout': nul.decode()})}))
        output = exec_output.ExecOutput(limit=1000)
        output.feed(1, nul)
        as_wire = jsonutils.dumps(msg_common.serialize_msg(
            {'result': output.as_wire()}))

        self.assertGreater(len(as_text), 7000)
        # stdout and output, each 4/3 of 1000
        self.assertLess(len(as_wire), 3000)


class TestCriExecOutput(base.TestCase):

    def setUp(self):
        super(TestCriExecOutput, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.config(exec_output_limit=1024, group='compute')

    def _run(self, out, err):
        with mock.patch.object(self.driver, '_exec_in_container',
                               return_value=(0, out, err)):
            return self.driver.execute_run('c1', ['cmd'])

    def test_streams_are_kept_apart_and_bounded(self):
        output, code = self._run(b'o' * 2000, b'err')

        self.assertEqual(0, code)
        self.assertEqual(b'o' * 1024, output.stdout)
        self.assertEqual(b'err', output.stderr)
        self.assertEqual(b'o' * 1024 + b'err', output.combined)
        self.assertEqual({'stdout'}, output.truncated)

    @mock.patch.object(cri_driver, 'RUNTIME_EXEC_STREAM_MAX', 2048)
    def test_what_the_runtime_cut_is_said_to_be_cut(self):
        # Within our limit, but exactly the runtime's own: it dropped the
        # rest, and the answer has to say so.
        self.config(exec_output_limit=4096, group='compute')
        output, _code = self._run(b'', b'e' * 2048)

        self.assertEqual(b'e' * 2048, output.stderr)
        self.assertEqual({'stderr'}, output.truncated)

    def test_the_channel_takes_both_streams_at_the_runtime_s_limit(self):
        self.assertGreater(cri_driver.MAX_RECEIVE_MESSAGE_LENGTH,
                           2 * cri_driver.RUNTIME_EXEC_STREAM_MAX)

    def test_an_answer_the_runtime_will_not_send_is_said_so(self):
        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.RESOURCE_EXHAUSTED
        with mock.patch.object(self.driver, '_exec_in_container',
                               side_effect=error):
            e = self.assertRaises(exception.Invalid,
                                  self.driver.execute_run, 'c1', ['cmd'])

        self.assertIn('larger than the container runtime will send', str(e))
