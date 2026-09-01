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

"""A node's docker network goes when the last container using it goes.

It is made on demand the first time a container on this host needs it,
and nothing removed it: the only remover was an admin API that targets
one host and that nothing calls. Removing it is what makes libnetwork
release the subnetpool the IPAM driver made for it, so every network
left behind was a pool never reclaimed -- measured as 26 networks and
54 pools on three nodes -- until the driver refused to make the next
one and no network could be created at all.
"""

from unittest import mock

from docker import errors

from zun.container.docker import driver as docker_driver
from zun.tests import base


def _not_found():
    response = mock.Mock(status_code=404)
    return errors.NotFound('no such network', response=response)


class TestReleaseWhenNothingUsesIt(base.TestCase):

    def setUp(self):
        super(TestReleaseWhenNothingUsesIt, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)
        self.docker = mock.Mock()
        self.row = mock.Mock()
        patcher = mock.patch.object(docker_driver.objects.ZunNetwork, 'list',
                                    return_value=[self.row])
        self.list = patcher.start()
        self.addCleanup(patcher.stop)

    def _release(self, nets=('net-1',)):
        self.driver._release_networks_left_unused(mock.Mock(), self.docker,
                                                  list(nets))

    def test_an_empty_network_is_removed_with_its_row(self):
        self.docker.inspect_network.return_value = {'Containers': {}}

        self._release()

        self.docker.remove_network.assert_called_once_with('net-1')
        self.row.destroy.assert_called_once()

    def test_a_network_still_in_use_is_left_alone(self):
        self.docker.inspect_network.return_value = {
            'Containers': {'abc': {'Name': 'other'}}}

        self._release()

        self.docker.remove_network.assert_not_called()
        self.row.destroy.assert_not_called()

    def test_it_asks_dockerd_not_the_database(self):
        """The two differ mid-delete; dockerd decides if remove succeeds."""
        self.docker.inspect_network.return_value = {'Containers': {}}

        self._release()

        self.docker.inspect_network.assert_called_once_with('net-1')

    def test_a_network_already_gone_still_drops_the_stale_row(self):
        self.docker.inspect_network.side_effect = _not_found()

        self._release()

        self.docker.remove_network.assert_not_called()
        self.row.destroy.assert_called_once()

    def test_each_network_the_container_was_on_is_considered(self):
        self.docker.inspect_network.return_value = {'Containers': {}}

        self._release(('net-1', 'net-2'))

        self.assertEqual(['net-1', 'net-2'],
                         [c[0][0] for c in
                          self.docker.remove_network.call_args_list])

    def test_the_row_looked_up_is_this_hosts(self):
        self.docker.inspect_network.return_value = {'Containers': {}}

        self._release()

        filters = self.list.call_args[1]['filters']
        self.assertEqual('net-1', filters['neutron_net_id'])
        self.assertEqual(docker_driver.CONF.host, filters['host'])


class TestProvisionAndReleaseShareALock(base.TestCase):
    """A create must not find the network present and then find it gone."""

    def test_the_lock_names_the_network(self):
        self.assertNotEqual(docker_driver._network_lock('a'),
                            docker_driver._network_lock('b'))
        self.assertIn('a', docker_driver._network_lock('a'))

    def test_provisioning_takes_it(self):
        driver = docker_driver.DockerDriver.__new__(docker_driver.DockerDriver)
        network_driver = mock.Mock()
        with mock.patch.object(docker_driver.lockutils, 'lock') as lock:
            driver._provision_network(mock.Mock(), network_driver,
                                      [{'network': 'net-1'}])
        lock.assert_called_once_with(docker_driver._network_lock('net-1'))
        network_driver.get_or_create_network.assert_called_once()


class TestDeleteReleases(base.TestCase):
    """The release runs on both ways out of delete: removed, or gone."""

    def setUp(self):
        super(TestDeleteReleases, self).setUp()
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
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(docker_driver.zun_network, 'driver')
        p.start()
        self.addCleanup(p.stop)
        self.container = mock.Mock(container_id='c1',
                                   addresses={'net-1': [], 'net-2': []})

    def test_after_a_removal(self):
        self.driver.delete(mock.Mock(), self.container, True)

        nets = self._release_networks_left_unused.call_args[0][2]
        self.assertEqual({'net-1', 'net-2'}, set(nets))

    def test_when_the_container_was_already_gone(self):
        self.docker.remove_container.side_effect = _not_found()

        self.driver.delete(mock.Mock(), self.container, True)

        self._release_networks_left_unused.assert_called_once()

    def test_not_when_delete_stopped_short(self):
        """The `not connected` branch returns before the container is
        removed, so the network is still in use and must not be touched.
        """
        response = mock.Mock(status_code=500)
        # The driver classifies by str(e), which docker-py builds from the
        # response and the explanation -- not from the message.
        self.docker.remove_container.side_effect = errors.APIError(
            'server error', response=response,
            explanation='container c1 is not connected to the network net-1')

        self.driver.delete(mock.Mock(), self.container, True)

        self._release_networks_left_unused.assert_not_called()
