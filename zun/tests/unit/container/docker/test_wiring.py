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

"""A started container must actually be wired into the host.

Measured in production: a container came up on a struggling node with
every status saying it was fine -- docker said running, the neutron port
said ACTIVE, the southbound binding said up -- while no interface for its
port existed on the host, and every packet to it went nowhere. The one
witness that does not lie is the interface in the host network namespace,
which this process shares.
"""

from unittest import mock

from zun.common import exception
from zun.container.docker import driver as docker_driver
from zun.tests import base

PORT = 'fef49709-768e-4472-94a2-fa9f740ae5ce'
TAP = 'tapfef49709-76'


def _container(**attrs):
    attrs.setdefault('uuid', 'u-1')
    attrs.setdefault('status_reason', None)
    attrs.setdefault('addresses', {
        'net-1': [{'addr': '10.0.0.5', 'version': 4, 'port': PORT}]})
    return mock.Mock(**attrs)


class WiringTest(base.TestCase):

    def setUp(self):
        super(WiringTest, self).setUp()
        self.driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)
        self.driver._unwired = set()
        self.config(group='docker', verify_wiring=True,
                    verify_wiring_timeout=1)

    def _exists(self, value):
        patcher = mock.patch.object(docker_driver.os.path, 'exists',
                                    return_value=value)
        handle = patcher.start()
        self.addCleanup(patcher.stop)
        return handle


class TestTheInterfaceName(WiringTest):

    def test_kuryrs_name_tap_plus_eleven_characters_of_the_port(self):
        found = self.driver._host_interfaces_of(_container())

        self.assertEqual([(PORT, TAP)], found)

    def test_a_dual_stack_port_is_one_interface(self):
        container = _container(addresses={'net-1': [
            {'addr': '10.0.0.5', 'version': 4, 'port': PORT},
            {'addr': 'fd00::5', 'version': 6, 'port': PORT}]})

        self.assertEqual(1, len(self.driver._host_interfaces_of(container)))

    def test_no_addresses_is_no_interfaces(self):
        self.assertEqual([], self.driver._host_interfaces_of(
            _container(addresses=None)))


class TestTheStartCheck(WiringTest):

    def test_a_wired_container_passes(self):
        self._exists(True)

        self.driver._assert_wired(_container())   # must not raise

    def test_an_unwired_container_is_refused_by_name(self):
        self._exists(False)
        with mock.patch.object(docker_driver.time, 'sleep'):
            error = self.assertRaises(exception.ZunException,
                                      self.driver._assert_wired, _container())

        self.assertIn('never wired', str(error))
        self.assertIn(TAP, str(error))
        self.assertIn(PORT, str(error))

    def test_off_means_off(self):
        self.config(group='docker', verify_wiring=False)
        asked = self._exists(False)

        self.driver._assert_wired(_container())

        asked.assert_not_called()


class TestTheRunningWatch(WiringTest):
    """Detection only, on the second consecutive miss."""

    def test_the_first_miss_is_noted_not_acted_on(self):
        self._exists(False)
        container = _container()

        self.driver._watch_wiring(container)

        container.save.assert_not_called()
        self.assertIn('u-1', self.driver._unwired)

    def test_the_second_miss_writes_the_truth_on_the_record(self):
        self._exists(False)
        container = _container()

        self.driver._watch_wiring(container)
        self.driver._watch_wiring(container)

        self.assertIn('not wired', container.status_reason)
        self.assertIn(TAP, container.status_reason)
        container.save.assert_called_once()

    def test_the_reason_is_not_rewritten_every_sweep(self):
        self._exists(False)
        container = _container()

        for _ in range(4):
            self.driver._watch_wiring(container)

        container.save.assert_called_once()

    def test_recovery_clears_the_strike(self):
        asked = self._exists(False)
        container = _container()
        self.driver._watch_wiring(container)
        asked.return_value = True

        self.driver._watch_wiring(container)

        self.assertNotIn('u-1', self.driver._unwired)
        container.save.assert_not_called()


class TestStartRefusesTheBlackHole(WiringTest):

    def test_a_start_that_wired_nothing_is_stopped_and_says_why(self):
        docker = mock.MagicMock()
        cm = mock.patch.object(docker_driver.docker_utils, 'docker_client')
        client = cm.start()
        self.addCleanup(cm.stop)
        client.return_value.__enter__.return_value = docker
        with mock.patch.object(docker_driver.zun_network, 'driver'), \
                mock.patch.object(self.driver, '_apply_volume_io_limits'), \
                mock.patch.object(
                    self.driver, '_assert_wired',
                    side_effect=exception.ZunException('never wired: x')):
            container = _container(container_id='c-1')
            self.driver.start(mock.Mock(), container)

        self.assertEqual(docker_driver.consts.STOPPED, container.status)
        self.assertIn('never wired', container.status_reason)
        docker.stop.assert_called_once()
