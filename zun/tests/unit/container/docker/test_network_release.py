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

from zun.common import exception
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
        # This host's row; another host's is what keeps a network.
        self.row = mock.Mock(host=docker_driver.CONF.host)
        patcher = mock.patch.object(docker_driver.objects.ZunNetwork, 'list',
                                    return_value=[self.row])
        self.list = patcher.start()
        self.addCleanup(patcher.stop)
        # neutron still has the network unless a test says otherwise.
        patcher = mock.patch.object(self.driver, '_neutron_network_is_gone',
                                    return_value=False)
        self.gone = patcher.start()
        self.addCleanup(patcher.stop)

    def _release(self, nets=('net-1',)):
        self.driver._release_networks_left_unused(mock.Mock(), self.docker,
                                                  list(nets))

    def test_an_empty_network_is_removed_with_its_row(self):
        self.docker.inspect_network.return_value = {'Containers': {}}

        self._release()

        self.docker.remove_network.assert_called_once_with('net-1')
        self.row.destroy.assert_called_once()

    def test_a_network_another_host_still_wraps_is_kept(self):
        """Its address pool is one neutron object shared by every host's
        docker network for the subnet; kuryr releases it with the network.
        Removing it here left the other hosts holding a dead pool id, and
        every start there failed until the network was made again."""
        self.docker.inspect_network.return_value = {'Containers': {}}
        self.list.return_value = [self.row,
                                  mock.Mock(host='another-host')]

        self._release()

        self.docker.remove_network.assert_not_called()
        self.row.destroy.assert_not_called()

    def test_a_network_neutron_no_longer_has_is_removed_wherever_it_is(self):
        """Every host's wrapper of it is garbage; the pool it holds too."""
        self.docker.inspect_network.return_value = {'Containers': {}}
        self.list.return_value = [self.row, mock.Mock(host='another-host')]
        self.gone.return_value = True

        self._release()

        self.docker.remove_network.assert_called_once_with('net-1')
        self.row.destroy.assert_called_once()

    def test_a_removal_dockerd_refuses_keeps_the_row(self):
        """Dropping it would leave the next sweep unable to find what
        this one left behind."""
        self.docker.inspect_network.return_value = {'Containers': {}}
        self.docker.remove_network.side_effect = errors.APIError(
            'kuryr said no', mock.Mock(status_code=500), None)

        self._release()

        self.row.destroy.assert_not_called()

    def test_the_last_host_wrapping_it_removes_it(self):
        self.docker.inspect_network.return_value = {'Containers': {}}
        self.list.return_value = [self.row]

        self._release()

        self.docker.remove_network.assert_called_once_with('net-1')

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


class TestNeutronIsAsked(base.TestCase):
    """Gone means neutron said so; not knowing keeps the wrapper."""

    def setUp(self):
        super(TestNeutronIsAsked, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)

    def _ask(self, side_effect=None):
        with mock.patch.object(docker_driver.neutron, 'NeutronAPI') as api:
            api.return_value.get_neutron_network.side_effect = side_effect
            return self.driver._neutron_network_is_gone(mock.Mock(), 'net-1')

    def test_a_network_neutron_cannot_find_is_gone(self):
        self.assertTrue(self._ask(exception.NetworkNotFound(network='net-1')))

    def test_a_network_neutron_has_is_not(self):
        self.assertFalse(self._ask())

    def test_a_neutron_that_cannot_be_asked_is_not_permission(self):
        self.assertFalse(self._ask(IOError('neutron unreachable')))


class TestTheSweep(base.TestCase):
    """The delete path's decision, applied to everything this node recorded.

    A delete that failed part way, or a neutron network removed while the
    wrapper sat empty, left the wrapper standing with nothing to remove it
    -- and the address pool kuryr made for it.
    """

    def setUp(self):
        super(TestTheSweep, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)
        self.docker = mock.MagicMock()
        cm = mock.patch.object(docker_driver.docker_utils, 'docker_client')
        client = cm.start()
        self.addCleanup(cm.stop)
        client.return_value.__enter__.return_value = self.docker
        p = mock.patch.object(self.driver, '_release_networks_left_unused')
        self.release = p.start()
        self.addCleanup(p.stop)

    def test_every_network_this_host_recorded_is_considered_once(self):
        rows = [mock.Mock(neutron_net_id='net-b'),
                mock.Mock(neutron_net_id='net-a'),
                mock.Mock(neutron_net_id='net-b')]
        with mock.patch.object(docker_driver.objects.ZunNetwork, 'list',
                               return_value=rows) as listed:
            self.driver.reclaim_stale_networks(mock.Mock())

        self.assertEqual({'host': docker_driver.CONF.host},
                         listed.call_args[1]['filters'])
        self.release.assert_called_once_with(mock.ANY, self.docker,
                                             ['net-a', 'net-b'])

    def test_nothing_recorded_asks_dockerd_nothing(self):
        with mock.patch.object(docker_driver.objects.ZunNetwork, 'list',
                               return_value=[]):
            self.driver.reclaim_stale_networks(mock.Mock())

        self.release.assert_not_called()

