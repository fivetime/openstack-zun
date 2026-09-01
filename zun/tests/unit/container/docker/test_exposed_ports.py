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

"""exposed_ports is a declaration, on the docker driver.

As docker's own --expose: recorded, shown, and it opens nothing. This
driver used to turn it into a security group of its own with the ports
open to 0.0.0.0/0 -- one group per container, created and deleted with
the container, which is the axis that costs a cloud-wide recompute --
against a default quota of ten groups. The CRI driver never did, so the
same request meant two different things on two hosts. Now it means the
same thing on both, and what can reach a container is decided by its
security groups alone.
"""

from unittest import mock

from zun.container.docker import driver as docker_driver
from zun import objects
from zun.tests import base


class DeclaredPortsTest(base.TestCase):

    def setUp(self):
        super(DeclaredPortsTest, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)

    def _container(self, exposed_ports, security_groups=None):
        container = mock.Mock(spec=objects.Container)
        container.exposed_ports = exposed_ports
        container.security_groups = security_groups
        return container

    def test_the_declaration_reaches_docker(self):
        kwargs = {}

        self.driver._declare_exposed_ports(
            self._container({'80/tcp': {}, '53/udp': {}}), kwargs)

        self.assertEqual({('80', 'tcp'), ('53', 'udp')}, set(kwargs['ports']))

    def test_it_opens_nothing(self):
        """No security group is made, and the container's own list is
        not touched -- neither the explicit groups nor their absence."""
        container = self._container({'80/tcp': {}}, security_groups=['sg-1'])

        self.driver._declare_exposed_ports(container, {})

        self.assertEqual(['sg-1'], container.security_groups)

    def test_absent_groups_stay_absent(self):
        """An absent list lets neutron pick the project default; the old
        code turned it into a one-element list, which does not."""
        container = self._container({'80/tcp': {}})

        self.driver._declare_exposed_ports(container, {})

        self.assertIsNone(container.security_groups)

    def test_nothing_declared_says_nothing(self):
        kwargs = {}

        self.driver._declare_exposed_ports(self._container(None), kwargs)

        self.assertNotIn('ports', kwargs)

    def test_a_capsule_declares_what_its_members_declare(self):
        capsule = mock.Mock(spec=objects.Capsule)
        capsule.init_containers = [self._container({'9000/tcp': {}})]
        capsule.containers = [self._container({'80/tcp': {}}),
                              self._container(None)]
        kwargs = {}

        self.driver._declare_exposed_ports(capsule, kwargs)

        self.assertEqual({('9000', 'tcp'), ('80', 'tcp')},
                         set(kwargs['ports']))


class DeleteLeavesSecurityGroupsAloneTest(base.TestCase):
    """The old cleanup deleted security_groups[0] of any container that
    declared a port, on the assumption that [0] was the group it had made.
    Nothing is made now, so nothing is deleted -- including a group the
    tenant added later, which is what [0] would have been."""

    def setUp(self):
        super(DeleteLeavesSecurityGroupsAloneTest, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)
        self.docker = mock.MagicMock()
        cm = mock.patch.object(docker_driver.docker_utils, 'docker_client')
        client = cm.start()
        self.addCleanup(cm.stop)
        client.return_value.__enter__.return_value = self.docker
        for name in ('_cleanup_network_for_container', '_remove_resolv_conf',
                     '_release_networks_left_unused'):
            p = mock.patch.object(docker_driver.DockerDriver, name)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(docker_driver.zun_network, 'driver')
        self.network_driver = p.start().return_value
        self.addCleanup(p.stop)

    def test_delete_does_not_touch_security_groups(self):
        container = mock.Mock(spec=objects.Container)
        container.container_id = 'c1'
        container.addresses = {}
        container.exposed_ports = {'80/tcp': {}}
        container.security_groups = ['the-tenants-own-group']

        self.driver.delete(mock.Mock(), container, True)

        self.network_driver.neutron_api.delete_security_group.\
            assert_not_called()
        self.assertEqual(['the-tenants-own-group'], container.security_groups)
