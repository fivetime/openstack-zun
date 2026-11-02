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

"""What a capsule container printed: kept bounded, and gone with it.

Real files in a real directory, because the behaviour is the files. The
runtime is the only fake: whether containerd really reopens the path is not
something a unit test can say.
"""

import os
import shutil
import tempfile
from unittest import mock

import grpc

from zun.compute import manager as compute_manager
from zun.container.cri import driver as cri_driver
from zun.container.cri import logfiles
from zun.criapi import api_pb2
from zun.tests import base


class _Refused(grpc.RpcError):
    def details(self):
        return 'container is not running'

    def code(self):
        return grpc.StatusCode.UNKNOWN


def _line(text):
    return ('2026-11-02T00:00:00.000000000Z stdout F %s\n' % text).encode()


class LogFilesBase(base.TestCase):

    def setUp(self):
        super(LogFilesBase, self).setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.config(cri_log_root=self.root)
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()

    def _write(self, name, data=b'x'):
        with open(os.path.join(self.root, name), 'wb') as fd:
            fd.write(data)

    def _names(self):
        return sorted(os.listdir(self.root))


class RemoveWithContainerTest(LogFilesBase):

    def _capsule(self, container_id='pod'):
        return mock.Mock(uuid='cap', container_id=container_id,
                         containers=[mock.Mock(uuid='main')],
                         init_containers=[mock.Mock(uuid='init')])

    def _delete(self, capsule, force=False):
        with mock.patch.object(self.driver, '_stop_in_pod_order'), \
                mock.patch.object(self.driver, '_delete_neutron_ports'):
            self.driver.delete_capsule(mock.Mock(), capsule, force)

    def test_capsule_delete_removes_every_member_and_rotated_file(self):
        for name in ('main.log', 'main.log.1', 'init.log', 'other.log',
                     'other.log.1'):
            self._write(name)
        with mock.patch.object(self.driver, '_delete_sandbox'):
            self._delete(self._capsule())
        self.assertEqual(['other.log', 'other.log.1'], self._names())

    def test_capsule_that_never_got_a_sandbox_is_still_cleaned(self):
        self._write('main.log')
        self._delete(self._capsule(container_id=None))
        self.assertEqual([], self._names())

    def test_failed_delete_keeps_logs_while_the_capsule_stays(self):
        self._write('main.log')
        with mock.patch.object(self.driver, '_delete_sandbox',
                               side_effect=RuntimeError('boom')):
            self.assertRaises(RuntimeError, self._delete, self._capsule())
        self.assertEqual(['main.log'], self._names())

    def test_failed_forced_delete_removes_them_as_the_record_goes(self):
        self._write('main.log')
        with mock.patch.object(self.driver, '_delete_sandbox',
                               side_effect=RuntimeError('boom')):
            self.assertRaises(RuntimeError, self._delete, self._capsule(),
                              force=True)
        self.assertEqual([], self._names())

    def test_a_file_that_will_not_go_does_not_fail_the_delete(self):
        self._write('main.log')
        with mock.patch.object(self.driver, '_delete_sandbox'), \
                mock.patch.object(logfiles.os, 'unlink',
                                  side_effect=PermissionError('no')):
            self._delete(self._capsule())

    def test_unloadable_member_list_does_not_fail_the_delete(self):
        capsule = mock.Mock(uuid='cap', container_id='pod')
        type(capsule).containers = mock.PropertyMock(
            side_effect=RuntimeError('no db'))
        with mock.patch.object(self.driver, '_delete_sandbox'):
            self._delete(capsule)

    def test_container_api_delete_removes_its_log(self):
        self._write('solo.log')
        container = mock.Mock(uuid='solo', container_id=None)
        with mock.patch.object(self.driver, '_sandbox_of',
                               return_value=None), \
                mock.patch.object(self.driver, '_delete_neutron_ports'):
            self.driver.delete(mock.Mock(), container, False)
        self.assertEqual([], self._names())


class RotateTest(LogFilesBase):

    def setUp(self):
        super(RotateTest, self).setUp()
        self.config(cri_log_max_size=1, cri_log_max_files=3)
        self.limit = 1024 * 1024
        self._running({'running': 'id-1'})

    def _running(self, by_uuid):
        stub = self.driver.runtime_stub
        stub.ListContainers.return_value = mock.Mock(
            containers=[mock.Mock(id=i) for i in by_uuid.values()])
        paths = {i: logfiles.current(self.root, u)
                 for u, i in by_uuid.items()}

        def status(request):
            return mock.Mock(status=mock.Mock(
                log_path=paths[request.container_id]))
        stub.ContainerStatus.side_effect = status

    def test_below_the_limit_nothing_is_asked_of_the_runtime(self):
        self._write('running.log', b'x' * (self.limit - 1))
        self.driver.rotate_logs()
        self.driver.runtime_stub.ListContainers.assert_not_called()
        self.assertEqual(['running.log'], self._names())

    def test_at_the_limit_moves_aside_and_reopens(self):
        self._write('running.log', b'x' * self.limit)
        self.driver.rotate_logs()
        self.assertEqual(['running.log.1'], self._names())
        self.driver.runtime_stub.ReopenContainerLog.assert_called_once_with(
            api_pb2.ReopenContainerLogRequest(container_id='id-1'))

    def test_only_running_containers_are_listed(self):
        self._write('running.log', b'x' * self.limit)
        self.driver.rotate_logs()
        request = self.driver.runtime_stub.ListContainers.call_args[0][0]
        self.assertEqual(api_pb2.CONTAINER_RUNNING, request.filter.state.state)

    def test_stopped_container_is_left_alone(self):
        self._write('stopped.log', b'x' * self.limit)
        self.driver.rotate_logs()
        self.assertEqual(['stopped.log'], self._names())
        self.driver.runtime_stub.ReopenContainerLog.assert_not_called()

    def test_keeps_no_more_than_max_files(self):
        self._write('running.log', b'new' + b'x' * self.limit)
        self._write('running.log.1', b'middle')
        self._write('running.log.2', b'oldest')
        self.driver.rotate_logs()
        self.assertEqual(['running.log.1', 'running.log.2'], self._names())
        with open(os.path.join(self.root, 'running.log.2'), 'rb') as fd:
            self.assertEqual(b'middle', fd.read())

    def test_refused_reopen_puts_the_file_back(self):
        self._write('running.log', b'x' * self.limit)
        self.driver.runtime_stub.ReopenContainerLog.side_effect = _Refused()
        self.driver.rotate_logs()
        self.assertEqual(['running.log'], self._names())

    def test_zero_never_rotates(self):
        self.config(cri_log_max_size=0)
        self._write('running.log', b'x')
        self.driver.rotate_logs()
        self.assertEqual(['running.log'], self._names())

    def test_a_status_that_fails_does_not_stop_the_others(self):
        self._running({'gone': 'id-0', 'running': 'id-1'})
        real = self.driver.runtime_stub.ContainerStatus.side_effect

        def status(request):
            if request.container_id == 'id-0':
                raise _Refused()
            return real(request)
        self.driver.runtime_stub.ContainerStatus.side_effect = status
        self._write('running.log', b'x' * self.limit)
        self.driver.rotate_logs()
        self.assertEqual(['running.log.1'], self._names())

    def test_log_root_with_a_trailing_slash_still_matches(self):
        self.config(cri_log_root=self.root + '/')
        self._write('running.log', b'x' * self.limit)
        self.driver.rotate_logs()
        self.assertEqual(['running.log.1'], self._names())

    def test_the_status_field_read_is_the_real_one(self):
        # The runtime is a mock above, so it would answer to any name.
        self.assertIn('log_path',
                      api_pb2.ContainerStatus.DESCRIPTOR.fields_by_name)

    def test_missing_directory_is_not_an_error(self):
        self.config(cri_log_root=os.path.join(self.root, 'nope'))
        self.driver.rotate_logs()


class ReadAcrossRotationTest(LogFilesBase):

    def _show(self, **kwargs):
        return self.driver.show_logs(mock.Mock(), mock.Mock(uuid='u'),
                                     **kwargs)

    def test_reads_oldest_first(self):
        self._write('u.log.2', _line('one'))
        self._write('u.log.1', _line('two'))
        self._write('u.log', _line('three'))
        self.assertEqual(b'one\ntwo\nthree\n', self._show())

    def test_tail_reaches_into_the_rotated_file(self):
        self._write('u.log.1', _line('one') + _line('two'))
        self._write('u.log', _line('three'))
        self.assertEqual(b'two\nthree\n', self._show(tail=2))

    def test_between_rename_and_reopen_only_the_rotated_file_exists(self):
        self._write('u.log.1', _line('one'))
        self.assertEqual(b'one\n', self._show())

    def test_no_file_is_empty_output(self):
        self.assertEqual(b'', self._show())

    def test_another_containers_files_are_not_read(self):
        self._write('u.log', _line('mine'))
        self._write('u2.log', _line('theirs'))
        self._write('u.log.bak', _line('not a rotation'))
        self.assertEqual(b'mine\n', self._show())


class RotationIsWiredTest(base.TestCase):
    """The periodic task, because a method nothing calls rotates nothing."""

    def setUp(self):
        super(RotationIsWiredTest, self).setUp()
        self.manager = compute_manager.Manager.__new__(compute_manager.Manager)
        self.manager._last_report = {}
        self.manager.capsule_driver = mock.Mock()

    def test_it_is_a_registered_periodic_task(self):
        names = [name for name, _task in
                 compute_manager.Manager._periodic_tasks]
        self.assertIn('rotate_container_logs', names)

    def test_rotates_once_per_interval(self):
        self.config(cri_log_rotate_interval=10)
        self.manager.rotate_container_logs(mock.Mock())
        self.manager.rotate_container_logs(mock.Mock())
        self.manager.capsule_driver.rotate_logs.assert_called_once_with()

    def test_a_failure_does_not_escape(self):
        self.manager.capsule_driver.rotate_logs.side_effect = RuntimeError('x')
        self.manager.rotate_container_logs(mock.Mock())

    def test_a_driver_with_no_rotation_is_skipped(self):
        self.manager.capsule_driver = mock.Mock(spec=[])
        self.manager.rotate_container_logs(mock.Mock())
