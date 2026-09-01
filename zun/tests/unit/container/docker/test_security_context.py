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

"""A capsule's securityContext, on the docker driver.

The CRI driver has applied it since the field arrived; this driver never
read it, so the same pod spec landed on a docker host as root, with a
writable root filesystem and every capability the image had -- and said
nothing. What was dropped is a tightening, which is the dangerous half of a
silent drop. The mapping here mirrors the CRI driver's field for field.
"""

from unittest import mock

from zun.container.docker import driver as docker_driver
from zun.tests import base


class SecurityContextTest(base.TestCase):

    def setUp(self):
        super(SecurityContextTest, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)
        self.config(allowed_capabilities=['NET_BIND_SERVICE'])

    def _apply(self, sc, user=None):
        container = mock.Mock(uuid='u-1', user=user,
                              healthcheck={'k8s_security_context': sc})
        kwargs = {'user': user} if user else {}
        host_config = {}
        self.driver._apply_security_context(container, kwargs, host_config)
        return kwargs, host_config

    def test_nothing_asked_sets_nothing(self):
        container = mock.Mock(uuid='u-1', healthcheck=None)
        kwargs, host_config = {}, {}

        self.driver._apply_security_context(container, kwargs, host_config)

        self.assertEqual({}, kwargs)
        self.assertEqual({}, host_config)

    def test_run_as_user_and_group_become_docker_user(self):
        kwargs, _hc = self._apply({'runAsUser': 1000, 'runAsGroup': 2000})

        self.assertEqual('1000:2000', kwargs['user'])

    def test_run_as_user_alone_leaves_the_group_to_the_image(self):
        kwargs, _hc = self._apply({'runAsUser': 1000})

        self.assertEqual('1000', kwargs['user'])

    def test_the_security_context_overrides_the_containers_own_user(self):
        """It is the more specific of the two requests, as on the CRI side."""
        kwargs, _hc = self._apply({'runAsUser': 1000}, user='nobody')

        self.assertEqual('1000', kwargs['user'])

    def test_a_group_alone_is_added_as_supplemental(self):
        """docker takes uid:gid only, and the uid is the image's to pick."""
        kwargs, hc = self._apply({'runAsGroup': 2000})

        self.assertNotIn('user', kwargs)
        self.assertEqual(['2000'], hc['group_add'])

    def test_fs_group_puts_the_process_in_the_group(self):
        """Chowning the volume to the group does nothing unless the process
        is in it; without this the volume mounts and still cannot be
        written."""
        _kw, hc = self._apply({'fsGroup': 3000})

        self.assertEqual(['3000'], hc['group_add'])

    def test_read_only_root_filesystem(self):
        _kw, hc = self._apply({'readOnlyRootFilesystem': True})

        self.assertTrue(hc['read_only'])

    def test_no_privilege_escalation_is_no_new_privileges(self):
        """Named the other way round in each system."""
        _kw, hc = self._apply({'allowPrivilegeEscalation': False})

        self.assertEqual(['no-new-privileges'], hc['security_opt'])

    def test_allowing_escalation_sets_nothing(self):
        """True is docker's default; saying it would be saying nothing."""
        _kw, hc = self._apply({'allowPrivilegeEscalation': True})

        self.assertNotIn('security_opt', hc)

    def test_capabilities_are_added_and_dropped(self):
        _kw, hc = self._apply({'capabilities': {
            'add': ['NET_BIND_SERVICE'], 'drop': ['ALL']}})

        self.assertEqual(['NET_BIND_SERVICE'], hc['cap_add'])
        self.assertEqual(['ALL'], hc['cap_drop'])

    def test_a_capability_the_host_does_not_allow_is_refused(self):
        """Second line of defence behind the API check, as on the CRI
        side: a forbidden capability in the stored spec does not reach
        the runtime. Dropping is never restricted."""
        _kw, hc = self._apply({'capabilities': {
            'add': ['SYS_ADMIN', 'NET_BIND_SERVICE'], 'drop': ['MKNOD']}})

        self.assertEqual(['NET_BIND_SERVICE'], hc['cap_add'])
        self.assertEqual(['MKNOD'], hc['cap_drop'])

    def test_only_refused_capabilities_add_nothing(self):
        _kw, hc = self._apply({'capabilities': {'add': ['SYS_ADMIN']}})

        self.assertNotIn('cap_add', hc)

    def test_unconfined_seccomp(self):
        _kw, hc = self._apply({'seccompProfile': {'type': 'Unconfined'}})

        self.assertEqual(['seccomp=unconfined'], hc['security_opt'])

    def test_runtime_default_seccomp_is_dockers_default(self):
        _kw, hc = self._apply({'seccompProfile': {'type': 'RuntimeDefault'}})

        self.assertNotIn('security_opt', hc)

    def test_localhost_seccomp_is_not_handed_to_the_runtime(self):
        """Refused at the API; if it arrives anyway, a tenant-named host
        path must not reach docker."""
        _kw, hc = self._apply({'seccompProfile': {'type': 'Localhost',
                                                  'localhostProfile': 'x'}})

        self.assertNotIn('security_opt', hc)

    def test_everything_at_once_lands_in_both_places(self):
        kwargs, hc = self._apply({
            'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 2000,
            'readOnlyRootFilesystem': True, 'allowPrivilegeEscalation': False,
            'capabilities': {'drop': ['ALL']},
            'seccompProfile': {'type': 'Unconfined'}})

        self.assertEqual('1000:1000', kwargs['user'])
        self.assertEqual(['2000'], hc['group_add'])
        self.assertTrue(hc['read_only'])
        self.assertEqual(['no-new-privileges', 'seccomp=unconfined'],
                         hc['security_opt'])
        self.assertEqual(['ALL'], hc['cap_drop'])
