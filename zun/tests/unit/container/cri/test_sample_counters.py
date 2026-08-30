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

"""The readings a bill is worked out from.

Taken on a schedule, one call for the whole host. Without this the
driver falls back to the base class, which answers nothing at all --
and nothing at all is what a tenant is then charged for the CPU and
the traffic they used.
"""

from unittest import mock

from zun.container.cri import driver as cri_driver
from zun.tests import base


def _value(number):
    return mock.Mock(value=number)


def _stat(container_id, cpu_ns=100, memory=2048):
    return mock.Mock(attributes=mock.Mock(id=container_id),
                     cpu=mock.Mock(timestamp=7,
                                   usage_core_nano_seconds=_value(cpu_ns)),
                     memory=mock.Mock(working_set_bytes=_value(memory)))


class SampleCountersTest(base.TestCase):

    def setUp(self):
        super(SampleCountersTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.containers = [mock.Mock(uuid='u-1', container_id='c-1'),
                           mock.Mock(uuid='u-2', container_id='c-2')]

    def _sample(self, stats=None, networks=None):
        if stats is None:
            stats = [_stat('c-1'), _stat('c-2', 200)]
        self.driver.runtime_stub.ListContainerStats.return_value = mock.Mock(
            stats=stats)
        with mock.patch.object(self.driver, '_sandbox_networks',
                               return_value=networks or {}):
            return self.driver.sample_counters({}, self.containers)

    def test_one_call_answers_for_every_container(self):
        found = self._sample()

        self.assertEqual({'u-1', 'u-2'}, set(found))
        self.assertEqual(
            1, self.driver.runtime_stub.ListContainerStats.call_count)

    def test_the_cpu_counter_is_carried_as_the_runtime_gave_it(self):
        found = self._sample()

        self.assertEqual(200, found['u-2']['cpu']['total_ns'])
        self.assertEqual(7, found['u-2']['timestamp'])

    def test_there_is_no_invented_system_figure(self):
        """A reader dividing by it would divide by something made up here."""
        found = self._sample()

        self.assertIsNone(found['u-1']['cpu']['system_ns'])

    def test_a_container_this_host_does_not_hold_is_ignored(self):
        found = self._sample(stats=[_stat('c-1'), _stat('someone-elses')])

        self.assertEqual({'u-1'}, set(found))

    def test_network_counters_are_attached_when_the_sandbox_had_them(self):
        found = self._sample(networks={'c-1': {'eth0': {'rx_bytes': 5,
                                                        'tx_bytes': 9}}})

        self.assertEqual(5, found['u-1']['networks']['eth0']['rx_bytes'])
        self.assertNotIn('networks', found['u-2'])

    def test_a_host_that_will_not_answer_reports_nothing_rather_than_zeros(
            self):
        self.driver.runtime_stub.ListContainerStats.side_effect = \
            cri_driver.grpc.RpcError()

        with mock.patch.object(self.driver, '_sandbox_networks',
                               return_value={}):
            self.assertEqual({}, self.driver.sample_counters(
                {}, self.containers))

    def test_containers_without_an_id_are_not_asked_about(self):
        self.containers = [mock.Mock(uuid='u-3', container_id=None)]

        self.assertEqual({}, self.driver.sample_counters({}, self.containers))
        self.driver.runtime_stub.ListContainerStats.assert_not_called()


class SandboxNetworksTest(base.TestCase):

    def setUp(self):
        super(SandboxNetworksTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()

    def _sandbox(self, sandbox_id, name='eth0', rx=11, tx=22, present=True):
        interface = mock.Mock(name_=name, rx_bytes=_value(rx),
                              tx_bytes=_value(tx))
        interface.name = name
        network = mock.Mock(default_interface=interface)
        network.HasField = lambda field: present
        return mock.Mock(attributes=mock.Mock(id=sandbox_id),
                         linux=mock.Mock(network=network))

    def test_a_sandbox_s_counters_reach_the_containers_in_it(self):
        self.driver.runtime_stub.ListPodSandboxStats.return_value = mock.Mock(
            stats=[self._sandbox('pod-1')])
        self.driver.runtime_stub.ListContainers.return_value = mock.Mock(
            containers=[mock.Mock(id='c-1', pod_sandbox_id='pod-1'),
                        mock.Mock(id='c-2', pod_sandbox_id='pod-other')])

        found = self.driver._sandbox_networks()

        self.assertEqual(11, found['c-1']['eth0']['rx_bytes'])
        self.assertNotIn('c-2', found)

    def test_a_sandbox_without_an_interface_is_left_out(self):
        self.driver.runtime_stub.ListPodSandboxStats.return_value = mock.Mock(
            stats=[self._sandbox('pod-1', present=False)])

        self.assertEqual({}, self.driver._sandbox_networks())

    def test_a_host_that_will_not_answer_costs_only_the_network_figures(self):
        self.driver.runtime_stub.ListPodSandboxStats.side_effect = \
            cri_driver.grpc.RpcError()

        self.assertEqual({}, self.driver._sandbox_networks())
