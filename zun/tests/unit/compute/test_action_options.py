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

"""API 1.54 through the compute RPC client and manager."""

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.compute import manager
from zun.compute import rpcapi
import zun.conf
from zun import objects
from zun.tests import base
from zun.tests.unit.db import utils


class TestRpcKeepsOldCallsOld(base.TestCase):
    """A new argument is sent only when used, so old nodes still answer."""

    def setUp(self):
        super(TestRpcKeepsOldCallsOld, self).setUp()
        # The node is up: the host check is not what is under test.
        service = mock.Mock(host='h')
        self._patch = [
            mock.patch.object(rpcapi.objects.ZunService, 'list_by_binary',
                              return_value=[service]),
            mock.patch.object(rpcapi.servicegroup, 'ServiceGroup')]
        for patcher in self._patch:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.api = rpcapi.API.__new__(rpcapi.API)
        self.api._cast = mock.Mock()
        self.api._call = mock.Mock()
        self.container = mock.Mock(host='h')

    def test_each_method_leaves_the_argument_out_when_unused(self):
        self.api.container_stop(None, self.container, 5)
        self.api.container_exec(None, self.container, 'c', True, False)
        self.api.container_put_archive(None, self.container, '/', 'd', True)
        self.api.container_commit(None, self.container, 'r', 't')
        for call in (self.api._cast.call_args_list +
                     self.api._call.call_args_list):
            for key in ('signal', 'detach', 'options'):
                self.assertNotIn(key, call.kwargs)

    def test_and_sends_it_when_used(self):
        self.api.container_stop(None, self.container, 5, signal='SIGINT')
        self.api.container_exec(None, self.container, 'c', True, False,
                                detach=True)
        self.api.container_commit(None, self.container, 'r', 't',
                                  options={'author': 'a'})
        self.assertEqual('SIGINT', self.api._cast.call_args.kwargs['signal'])
        calls = self.api._call.call_args_list
        self.assertTrue(calls[0].kwargs['detach'])
        self.assertEqual({'author': 'a'}, calls[1].kwargs['options'])


class TestManagerOptions(base.TestCase):

    def setUp(self):
        super(TestManagerOptions, self).setUp()
        p = mock.patch('zun.scheduler.client.report.SchedulerReportClient')
        p.start()
        self.addCleanup(p.stop)
        zun.conf.CONF.set_override('container_driver', 'fake')
        zun.conf.CONF.set_override('capsule_driver', 'fake')
        self.manager = manager.Manager()
        self.driver = mock.Mock()
        self.manager.driver = self.driver
        self.container = objects.Container(
            self.context, **utils.get_test_container(status=consts.RUNNING))

    def _commit(self, options):
        with mock.patch.object(manager, '_credential_for'), \
                mock.patch.object(manager.utils, 'spawn_n'), \
                mock.patch.object(objects.Container, 'save'):
            self.driver.pause.return_value = self.container
            self.driver.unpause.return_value = self.container
            return self.manager._commit_to_registry(
                self.context, self.container, 'r.example.com/p/app', '1',
                options)

    def test_commit_options_reach_the_driver_after_the_pause(self):
        self._commit({'author': 'a', 'changes': ['ENV A=1']})

        self.driver.pause.assert_called_once()
        self.driver.commit.assert_called_once_with(
            self.context, self.container, 'r.example.com/p/app', '1',
            pause=True, author='a', changes=['ENV A=1'])

    def test_pause_false_skips_the_pause_and_says_so_to_the_driver(self):
        self._commit({'pause': False})

        self.driver.pause.assert_not_called()
        self.driver.commit.assert_called_once_with(
            self.context, self.container, 'r.example.com/p/app', '1',
            pause=False)

    def test_no_options_is_the_call_it_always_was(self):
        self._commit(None)

        self.driver.pause.assert_called_once()
        self.driver.commit.assert_called_once_with(
            self.context, self.container, 'r.example.com/p/app', '1')

    def test_options_on_the_image_service_path_are_refused(self):
        self.assertRaises(exception.Invalid, self.manager.container_commit,
                          self.context, self.container, 'app', '1',
                          options={'author': 'a'})

    def test_exec_detached_returns_at_once_with_the_exec_id(self):
        with mock.patch.object(self.manager, '_get_driver',
                               return_value=self.driver):
            self.driver.execute_create.return_value = 'e1'
            answer = self.manager.container_exec(
                self.context, self.container, 'sleep 9', True, False,
                detach=True)

        self.driver.execute_detached.assert_called_once_with('e1')
        self.driver.execute_run.assert_not_called()
        self.assertEqual({'output': None, 'exit_code': None,
                          'exec_id': 'e1', 'token': None}, answer)

    def test_put_archive_options_reach_the_driver(self):
        self.manager.container_put_archive(
            self.context, self.container, '/x', b'tar', False,
            options={'copy_uidgid': True})

        self.driver.put_archive.assert_called_once_with(
            self.context, self.container, '/x', b'tar', copy_uidgid=True)
