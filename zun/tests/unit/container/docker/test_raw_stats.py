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

"""The counters, before anything is computed from them.

Built from a reading taken off a live container on a kata host, so the
shapes here are the ones the runtime actually sends -- including the
block-I/O placeholder, which is what made a zero look like a measurement.
"""

from unittest import mock

from zun.container.docker import driver as docker_driver
from zun.tests import base

# Taken from a container running under kata-qemu on a 32-core host.
LIVE_READING = {
    'read': '2026-08-26T08:40:00.000000000Z',
    'preread': '2026-08-26T08:39:59.000000000Z',
    'cpu_stats': {
        'cpu_usage': {'total_usage': 26544000, 'percpu_usage': []},
        'system_cpu_usage': 3111200150000000,
        'online_cpus': 32,
    },
    'precpu_stats': {
        'cpu_usage': {'total_usage': 26544000},
        'system_cpu_usage': 3111168090000000,
    },
    'memory_stats': {
        'usage': 1589248, 'limit': 536870912,
        'stats': {'inactive_file': 4096},
    },
    'networks': {'eth0': {'rx_bytes': 1234, 'tx_bytes': 5678,
                          'rx_packets': 12, 'tx_packets': 34}},
    # What a VM runtime sends when nothing accounted for block I/O: the
    # entries are there, with no device and a zero.
    'blkio_stats': {'io_service_bytes_recursive': [
        {'major': 0, 'minor': 0, 'op': 'read', 'value': 0},
        {'major': 0, 'minor': 0, 'op': 'write', 'value': 0},
    ]},
    'pids_stats': {'current': 1},
}


class RawStatsTest(base.TestCase):

    def _raw(self, reading):
        driver = docker_driver.DockerDriver.__new__(
            docker_driver.DockerDriver)
        client = mock.MagicMock()
        client.stats.return_value = reading
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = client
        with mock.patch.object(docker_driver.docker_utils, 'docker_client',
                               return_value=ctx):
            return docker_driver.DockerDriver.raw_stats.__wrapped__(
                driver, {}, mock.Mock(container_id='c1'))

    def test_the_cpu_counters_come_through_with_their_predecessors(self):
        """A rate needs two readings; docker already kept the first."""
        raw = self._raw(LIVE_READING)

        self.assertEqual(26544000, raw['cpu']['total_ns'])
        self.assertEqual(26544000, raw['cpu']['previous_total_ns'])
        self.assertEqual(3111200150000000, raw['cpu']['system_ns'])
        self.assertEqual(3111168090000000, raw['cpu']['previous_system_ns'])
        self.assertEqual(32, raw['cpu']['online_cpus'])

    def test_memory_carries_the_cache_so_a_reader_can_subtract_it(self):
        raw = self._raw(LIVE_READING)

        self.assertEqual(1589248, raw['memory']['usage'])
        self.assertEqual(536870912, raw['memory']['limit'])
        self.assertEqual(4096, raw['memory']['cache'])

    def test_networks_come_through_when_there_are_any(self):
        raw = self._raw(LIVE_READING)

        self.assertEqual(1234, raw['networks']['eth0']['rx_bytes'])

    def test_placeholder_block_io_is_left_out_rather_than_reported_as_zero(
            self):
        """Absent says nobody looked; zero says the disk was idle."""
        raw = self._raw(LIVE_READING)

        self.assertNotIn('blkio', raw)

    def test_real_block_io_is_kept(self):
        reading = dict(LIVE_READING, blkio_stats={
            'io_service_bytes_recursive': [
                {'major': 253, 'minor': 0, 'op': 'Read', 'value': 8192},
                {'major': 253, 'minor': 0, 'op': 'Write', 'value': 4096},
            ]})
        raw = self._raw(reading)

        self.assertEqual(8192, raw['blkio']['read_bytes'])
        self.assertEqual(4096, raw['blkio']['write_bytes'])

    def test_a_container_with_no_network_reports_none_rather_than_empty(self):
        """The CNI case zun's own code already noted."""
        reading = dict(LIVE_READING)
        reading.pop('networks')
        raw = self._raw(reading)

        self.assertNotIn('networks', raw)
