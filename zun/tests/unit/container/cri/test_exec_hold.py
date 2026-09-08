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

"""An exec whose command leaves its output open must not take the node down.

`sh -c "sleep 365d &"` exits at once, but the child keeps the write end of
the exec's output pipe, and the runtime waits for that pipe to close before
it answers. Nothing in the request bounds that wait. Measured on the
testbed: the ExecSync blocked for the two minutes the child lived, and
because a gRPC call blocks in C, zun-compute logged nothing at all for
those two minutes -- no other request, no periodic task, no heartbeat.
"""

from unittest import mock

from eventlet import tpool
import grpc

from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


class _RpcError(grpc.RpcError):

    def __init__(self, code, details):
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


_HELD = ('failed to drain exec process "abc" io: failed to drain exec '
         'process "abc" io in 5s because io is still held by other processes')


class ExecDeadlineTest(base.TestCase):

    def setUp(self):
        super(ExecDeadlineTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.exec_sync = self.driver.runtime_stub.ExecSync
        self.exec_sync.return_value = mock.Mock(exit_code=0, stdout=b'ok\n',
                                                stderr=b'')

    def test_the_call_ends_after_the_command_s_timeout_whatever_happens(self):
        self.driver._exec_in_container('abc', ['true'], 30)

        request = self.exec_sync.call_args.args[0]
        self.assertEqual(30, request.timeout)
        deadline = self.exec_sync.call_args.kwargs['timeout']
        # Later than the runtime's own kill, so a runtime that answers
        # gets to say why; earlier than the RPC reply timeout, so the API
        # relays the reason instead of a 504.
        self.assertGreater(deadline, 30)
        self.assertLess(deadline, 60)

    def test_a_probe_gets_the_same_bound(self):
        probe = {'exec': {'command': ['true']}, 'timeoutSeconds': 3}
        container = mock.Mock(container_id='abc')

        self.driver._run_probe(container, probe)

        self.assertGreater(self.exec_sync.call_args.kwargs['timeout'],
                           self.exec_sync.call_args.args[0].timeout)


class HeldOutputTest(base.TestCase):

    def setUp(self):
        super(HeldOutputTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.exec_sync = self.driver.runtime_stub.ExecSync

    def _run(self):
        return self.driver.execute_run('abc', ['sh', '-c', 'sleep 365d &'])

    def test_held_output_is_told_apart_from_a_command_that_ran_too_long(self):
        self.exec_sync.side_effect = _RpcError(grpc.StatusCode.UNKNOWN, _HELD)

        e = self.assertRaises(exception.Invalid, self._run)

        self.assertIn('holding its output open', str(e))
        self.assertIn('>/dev/null 2>&1 &', str(e))
        self.assertNotIn('did not finish', str(e))

    def test_a_command_that_ran_too_long_still_says_so(self):
        self.exec_sync.side_effect = _RpcError(
            grpc.StatusCode.DEADLINE_EXCEEDED, 'Deadline Exceeded')

        e = self.assertRaises(exception.Invalid, self._run)

        self.assertIn('did not finish', str(e))

    def test_any_other_failure_is_not_dressed_up(self):
        self.exec_sync.side_effect = _RpcError(grpc.StatusCode.UNKNOWN,
                                               'container not running')

        self.assertRaises(grpc.RpcError, self._run)

    def test_a_probe_whose_output_is_held_fails_rather_than_hangs(self):
        self.exec_sync.side_effect = _RpcError(grpc.StatusCode.UNKNOWN, _HELD)
        probe = {'exec': {'command': ['sh', '-c', 'sleep 365d &']}}

        self.assertFalse(self.driver._run_probe(
            mock.Mock(container_id='abc'), probe))


class OffHubTest(base.TestCase):
    """Every unary stub runs its calls on a native thread.

    The check is on the type: a stub that is not a Proxy blocks the hub on
    every call, and no unit test would notice, since a mock never blocks.
    """

    def _driver(self):
        with mock.patch.object(cri_driver.grpc, 'insecure_channel'):
            with mock.patch.object(cri_driver.img_driver,
                                   'load_image_driver'):
                with mock.patch.object(cri_driver.CONF, 'image_driver_list',
                                       []):
                    return cri_driver.CriDriver()

    def test_the_stubs_the_capsule_path_uses_run_off_the_hub(self):
        driver = self._driver()

        for name in ('runtime_stub', 'image_stub', 'task_stub',
                     'snapshot_stub'):
            self.assertIsInstance(getattr(driver, name), tpool.Proxy, name)

    def test_the_streaming_stubs_a_commit_uses_stay_direct(self):
        driver = self._driver()

        for name in ('content_stub', 'diff_stub', 'ctrd_image_stub'):
            self.assertNotIsInstance(getattr(driver, name), tpool.Proxy,
                                     name)

    def test_a_call_through_the_proxy_reaches_the_stub(self):
        driver = self._driver()
        driver.runtime_stub._obj.Version = mock.Mock(return_value='v')

        self.assertEqual('v', driver.runtime_stub.Version('req'))
