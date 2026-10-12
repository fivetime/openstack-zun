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

from unittest import mock

import fixtures
import oslo_service.backend as service_backend

from zun import monkey_patch
from zun.tests import base


class TestMonkeyPatch(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        # Every test starts from a process that has not chosen yet, and
        # none of them may really patch the test runner.
        # (fixtures.MockPatchObject would read new=None as "use a mock")
        p = mock.patch.object(monkey_patch, '_MODE', None)
        p.start()
        self.addCleanup(p.stop)
        self.monkey_patch = self.useFixture(
            fixtures.MockPatch('eventlet.monkey_patch')).mock
        self.init_backend = self.useFixture(fixtures.MockPatchObject(
            service_backend, 'init_backend')).mock

    def _patch_with(self, value):
        env = {} if value is None else {monkey_patch.ENV_VAR: value}
        with mock.patch.dict('os.environ', env, clear=True):
            monkey_patch.patch()

    def test_eventlet_by_default(self):
        self._patch_with(None)
        self.monkey_patch.assert_called_once_with()
        self.init_backend.assert_called_once_with(
            service_backend.BackendType.EVENTLET)
        self.assertFalse(monkey_patch.concurrency_mode_threading())

    def test_threading_when_disabled(self):
        for value in ('1', 'true', 'True', 'yes', ' TRUE '):
            monkey_patch._MODE = None
            self.init_backend.reset_mock()
            self._patch_with(value)
            self.monkey_patch.assert_not_called()
            self.init_backend.assert_called_once_with(
                service_backend.BackendType.THREADING)
            self.assertTrue(monkey_patch.concurrency_mode_threading())

    def test_other_values_keep_eventlet(self):
        for value in ('', '0', 'false', 'no'):
            monkey_patch._MODE = None
            self.monkey_patch.reset_mock()
            self._patch_with(value)
            self.monkey_patch.assert_called_once_with()
            self.assertFalse(monkey_patch.concurrency_mode_threading())

    def test_patch_is_idempotent(self):
        self._patch_with(None)
        self._patch_with('true')
        # The second call does not switch a process already patched.
        self.monkey_patch.assert_called_once_with()
        self.init_backend.assert_called_once_with(
            service_backend.BackendType.EVENTLET)
        self.assertFalse(monkey_patch.concurrency_mode_threading())

    def test_unpatched_process_is_threading(self):
        # The API under a WSGI server, the CNI daemon and the tests never
        # call patch(); nothing patched them, so they are on threads.
        self.assertTrue(monkey_patch.concurrency_mode_threading())

    def test_grpc_fork_support_is_off_by_default(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            monkey_patch.patch()
            import os
            self.assertEqual('false', os.environ['GRPC_ENABLE_FORK_SUPPORT'])

    def test_an_operators_grpc_fork_setting_wins(self):
        env = {'GRPC_ENABLE_FORK_SUPPORT': 'true'}
        with mock.patch.dict('os.environ', env, clear=True):
            monkey_patch.patch()
            import os
            self.assertEqual('true', os.environ['GRPC_ENABLE_FORK_SUPPORT'])
