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

from zun.common import consts
from zun.common import exception
from zun.compute import manager
from zun.compute import reasons
import zun.conf
from zun import objects
from zun.tests import base as test_base
from zun.tests.unit.db import base
from zun.tests.unit.db import utils

# Captured from a production create on 2026-11-05.
PULL_NO_HOST = (
    '<_InactiveRpcError of RPC that terminated with:\n'
    '\tstatus = StatusCode.UNKNOWN\n'
    '\tdetails = "failed to pull and unpack image '
    '"no.such.registry.invalid/x:1": failed to resolve image: failed to do '
    'request: Head "https://no.such.registry.invalid/v2/x/manifests/1": dial '
    'tcp: lookup no.such.registry.invalid on 127.0.0.53:53: no such host"\n>')
KATA_NO_COMMAND = (
    '<_InactiveRpcError of RPC that terminated with:\n'
    '\tdetails = "failed to create containerd task: failed to create shim '
    'task: Others("failed to handle message create container\\n\\nCaused by:'
    '\\n    0: agent create container\\n    1: rpc status: Status { code: '
    'INTERNAL, message: \\"the file /no/such/binary was not found\\n\\nStack '
    'backtrace:\\n   0: <unknown>\\n"\n>')
RUNC_NO_COMMAND = (
    'Docker internal error: 400 Client Error for http+docker://localhost/'
    'v1.44/containers/dff6e61e/start: Bad Request ("failed to create task: '
    'OCI runtime create failed: runc create failed: unable to start container '
    'process: error during container init: exec: "/no/such/binary": stat '
    '/no/such/binary: no such file or directory").')
DOCKER_PULL_DENIED = (
    'Docker internal error: 500 Server Error for http+docker://localhost/'
    'v1.44/images/create: pull access denied for x, repository does not '
    'exist or may require docker login.')
MANIFEST_UNKNOWN = (
    'failed to pull and unpack image "harbor.internal.example/x:1": failed '
    'to resolve image: harbor.internal.example/x:1: not found')


class ForTenantTest(test_base.BaseTestCase):

    FALLBACK = 'The container could not be created'

    def _reason(self, text):
        return reasons.for_tenant(RuntimeError(text), self.FALLBACK)

    def test_pull_failures_say_what_a_tenant_can_act_on(self):
        self.assertEqual(reasons.REGISTRY_UNREACHABLE,
                         self._reason(PULL_NO_HOST))
        self.assertEqual(reasons.IMAGE_DENIED,
                         self._reason(DOCKER_PULL_DENIED))
        self.assertEqual(reasons.IMAGE_NOT_FOUND,
                         self._reason(MANIFEST_UNKNOWN))

    def test_missing_command_is_named_on_both_runtimes(self):
        self.assertEqual(reasons.COMMAND_NOT_FOUND,
                         self._reason(KATA_NO_COMMAND))
        self.assertEqual(reasons.COMMAND_NOT_FOUND,
                         self._reason(RUNC_NO_COMMAND))

    def test_command_permission(self):
        self.assertEqual(reasons.COMMAND_DENIED, self._reason(
            'error during container init: exec: "/x": permission denied'))

    def test_a_missing_file_elsewhere_is_not_a_missing_command(self):
        self.assertEqual(self.FALLBACK, self._reason(
            'open /run/containerd/x/config.json: no such file or directory'))

    def test_anything_else_is_the_fallback(self):
        self.assertEqual(self.FALLBACK, self._reason(
            'cni plugin failed: dial 10.0.0.1:6642: connection refused'))

    def test_what_was_written_for_the_tenant_passes(self):
        for error, text in (
                (exception.VolumeNotPrepared('share is too open'),
                 'share is too open'),
                (exception.InitContainerFailed(name='a', detail='exit:3'),
                 'Init container a failed with exit:3'),
                (exception.Invalid('a group has to be given by number'),
                 'a group has to be given by number')):
            self.assertEqual(text, reasons.for_tenant(error, self.FALLBACK))

    def test_our_own_exception_with_a_path_does_not_pass(self):
        error = exception.ZunException(
            'Could not write 100 to /sys/fs/cgroup/zun-x.slice/io.max: EPERM')
        self.assertEqual(self.FALLBACK,
                         reasons.for_tenant(error, self.FALLBACK))

    def test_claim_does_not_say_what_the_node_has(self):
        error = exception.ResourcesUnavailable(
            reason='Free memory 1024 MiB < requested 2048 MiB')
        self.assertEqual(reasons.NO_ROOM,
                         reasons.for_tenant(error, self.FALLBACK))


class ManagerReasonsTest(base.DbTestCase):
    """Each failing step of a real manager records a reason a tenant may read.
    """

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.driver = mock.Mock()
        self.manager.capsule_driver = self.driver
        p = mock.patch.object(self.manager, '_get_driver',
                              return_value=self.driver)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.manager, '_detach_volumes')
        p.start()
        self.addCleanup(p.stop)
        for cls in (objects.Capsule, objects.Container,
                    objects.CapsuleContainer, objects.CapsuleInitContainer):
            p = mock.patch.object(cls, 'save')
            p.start()
            self.addCleanup(p.stop)
        self.member = objects.CapsuleContainer(
            self.context, **utils.get_test_container(
                uuid='5d8c1a2b-3f4e-4a6b-8c9d-0e1f2a3b4c5d'))
        self.member.status = consts.CREATING
        self.init = objects.CapsuleInitContainer(
            self.context, **utils.get_test_container(
                uuid='6e9d2b3c-4a5f-4b7c-9d0e-1f2a3b4c5d6e'))
        self.init.status = consts.STOPPED
        self.capsule = objects.Capsule(
            self.context, **utils.get_test_container())
        self.capsule.status = consts.CREATING
        self.capsule.auto_remove = False
        self.capsule.containers = [self.member]
        self.capsule.init_containers = [self.init]

    def test_failed_capsule_does_not_leave_a_member_creating(self):
        self.manager._fail_container(self.context, self.capsule, 'why',
                                     unset_host=True)

        self.assertEqual(consts.ERROR, self.member.status)
        self.assertEqual('why', self.member.status_reason)
        # One that already finished keeps what it finished as.
        self.assertEqual(consts.STOPPED, self.init.status)

    def _failed_create(self, text):
        self.driver.create_capsule.side_effect = RuntimeError(text)
        self.driver.pulls_own_images = True
        self.assertRaises(
            RuntimeError, self.manager._do_container_create_base,
            self.context, self.capsule, [], {})

    def test_create_failure_says_nothing_of_the_node(self):
        self._failed_create(PULL_NO_HOST)

        self.assertEqual(consts.ERROR, self.capsule.status)
        self.assertEqual(reasons.REGISTRY_UNREACHABLE,
                         self.capsule.status_reason)

    def test_unknown_create_failure(self):
        self._failed_create('dial 10.0.0.1:6642: connection refused')

        self.assertEqual(reasons.NOT_CREATED, self.capsule.status_reason)
        self.assertEqual(reasons.NOT_CREATED, self.member.status_reason)

    def test_failed_init_container_keeps_its_sentence(self):
        # kubezun finds the exit code in this sentence.
        self.driver.create_capsule.side_effect = (
            exception.InitContainerFailed(name='setup', detail='exit:3'))
        self.driver.pulls_own_images = True
        self.assertRaises(
            exception.InitContainerFailed,
            self.manager._do_container_create_base,
            self.context, self.capsule, [], {})

        self.assertEqual('Init container setup failed with exit:3',
                         self.capsule.status_reason)

    def test_resumed_capsule_that_failed(self):
        self.manager._capsule_resumed(
            self.context, False, self.capsule, RuntimeError(PULL_NO_HOST))

        self.assertEqual(reasons.REGISTRY_UNREACHABLE,
                         self.capsule.status_reason)
