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

"""Attaching a network to a running container under a VM runtime.

docker adds the veth to the netns on the host and reports success;
nothing crosses into the guest, whose interfaces were fixed when the
sandbox booted. Measured on kata: the container keeps exactly the
interfaces and addresses it had while the API and `inspect` both say
the network is attached. Stopped, the same request works, because the
address is applied when the sandbox next boots.
"""

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.container.docker import driver as docker_driver
from zun.tests import base


class NoHotplugOnAVmTest(base.TestCase):

    def setUp(self):
        super(NoHotplugOnAVmTest, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)

    def _container(self, status=consts.RUNNING, runtime='kata-qemu'):
        return mock.Mock(uuid='u-1', status=status, runtime=runtime)

    def _refuse(self, container, verb='attach'):
        with mock.patch.object(self.driver, '_is_runtime_supported',
                               return_value=True):
            return self.driver._refuse_hotplug_on_a_vm(container, verb)

    def test_a_running_kata_container_is_refused(self):
        error = self.assertRaises(exception.Invalid, self._refuse,
                                  self._container())

        self.assertIn('kata-qemu', str(error))
        self.assertIn('Stop the container', str(error))

    def test_the_refusal_names_what_was_asked_for(self):
        error = self.assertRaises(exception.Invalid, self._refuse,
                                  self._container(), 'detach')

        self.assertIn('detach a network', str(error))

    def test_a_stopped_container_is_allowed(self):
        """It works stopped: the address is applied when the sandbox boots."""
        self._refuse(self._container(status=consts.STOPPED))

    def test_runc_is_allowed_because_it_really_hotplugs(self):
        self._refuse(self._container(runtime='runc'))

    def test_a_host_without_runtime_support_is_treated_as_runc(self):
        container = self._container()
        with mock.patch.object(self.driver, '_is_runtime_supported',
                               return_value=False):
            self.driver._refuse_hotplug_on_a_vm(container, 'attach')
