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

"""containerd runs a privileged container only in a sandbox marked privileged."""

from unittest import mock

from zun.container.cri import driver as cri_driver
from zun.tests import base


class PrivilegedSandboxTest(base.TestCase):

    def setUp(self):
        super(PrivilegedSandboxTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        patcher = mock.patch.object(cri_driver.CriDriver,
                                    '_sandbox_cgroup_parent',
                                    return_value='/zun')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self, containers=(), init_containers=()):
        capsule = mock.Mock(uuid='c-1', hostname=None, cpu=None, memory=None,
                            containers=list(containers),
                            init_containers=list(init_containers))
        return self.driver._get_sandbox_config(capsule)

    def _member(self, privileged):
        return mock.Mock(privileged=privileged, cpu=None, memory=None)

    def test_a_privileged_container_marks_the_sandbox(self):
        config = self._config(containers=[self._member(False),
                                          self._member(True)])
        self.assertTrue(config.linux.security_context.privileged)

    def test_a_privileged_init_container_marks_the_sandbox(self):
        config = self._config(containers=[self._member(None)],
                              init_containers=[self._member(True)])
        self.assertTrue(config.linux.security_context.privileged)

    def test_none_privileged_leaves_it_unmarked(self):
        config = self._config(containers=[self._member(None)],
                              init_containers=[self._member(False)])
        self.assertFalse(config.linux.security_context.privileged)

    def test_a_lone_container_is_its_own_member(self):
        container = mock.Mock(uuid='c-1', hostname=None, cpu=None, memory=None,
                              privileged=True, spec=['uuid', 'hostname', 'cpu',
                                                     'memory', 'privileged'])
        config = self.driver._get_sandbox_config(container)
        self.assertTrue(config.linux.security_context.privileged)
