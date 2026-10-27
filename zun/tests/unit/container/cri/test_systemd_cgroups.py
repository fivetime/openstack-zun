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

"""A runtime that files its cgroups through systemd.

runc told SystemdCgroup=true refuses a cgroup path ("expected cgroupsPath
to be of format slice:prefix:name"), and systemd puts back to "max" any
limit written into a slice's files by hand -- when the slice starts and on
every daemon-reload. Measured both; only the slice's own properties hold.
"""

import json
from unittest import mock

import grpc

from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.container.cri import hostio
from zun.tests import base

UUID = 'ea8e2a25-2901-438d-8157-de7ffd68d051'
SLICE = 'zun-ea8e2a25_2901_438d_8157_de7ffd68d051.slice'

CONTAINERD_CONFIG = {'containerd': {'runtimes': {
    'runc': {'options': {'SystemdCgroup': True}},
    'kata-qemu': {'options': {'ConfigPath': '/etc/kata/q.toml'}},
}}}


class _RpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE


class SystemdCgroupsTest(base.TestCase):

    def setUp(self):
        super(SystemdCgroupsTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.driver.runtime_stub.Status.return_value = mock.Mock(
            info={'config': json.dumps(CONTAINERD_CONFIG)})
        self.capsule = mock.Mock(uuid=UUID, runtime='runc',
                                 containers=[mock.Mock(cpu=0.5, memory=256)],
                                 init_containers=[])
        p = mock.patch.object(cri_driver.utils, 'execute',
                              return_value=('', ''))
        self.execute = p.start()
        self.addCleanup(p.stop)

    def test_runc_on_systemd_is_given_a_slice(self):
        self.assertEqual(SLICE,
                         self.driver._sandbox_cgroup_parent(self.capsule))

    def test_kata_keeps_its_path(self):
        self.capsule.runtime = 'kata-qemu'

        self.assertEqual('/zun.slice/%s' % UUID,
                         self.driver._sandbox_cgroup_parent(self.capsule))

    def test_containerd_is_asked_once(self):
        for _ in range(3):
            self.driver._sandbox_cgroup_parent(self.capsule)

        self.assertEqual(1, self.driver.runtime_stub.Status.call_count)

    def test_an_unreadable_config_is_asked_again(self):
        self.driver.runtime_stub.Status.side_effect = [_RpcError(),
                                                       mock.DEFAULT]

        self.assertEqual('/zun.slice/%s' % UUID,
                         self.driver._sandbox_cgroup_parent(self.capsule))
        self.assertEqual(SLICE,
                         self.driver._sandbox_cgroup_parent(self.capsule))

    def test_the_setting_wins_over_containerd(self):
        self.config(cri_cgroup_driver='cgroupfs')

        self.assertEqual('/zun.slice/%s' % UUID,
                         self.driver._sandbox_cgroup_parent(self.capsule))
        self.driver.runtime_stub.Status.assert_not_called()

    def test_the_ceiling_is_the_slices_own_properties(self):
        self.driver._apply_pod_ceiling(self.capsule, 'runc')

        self.execute.assert_called_once_with(
            'systemctl', 'set-property', '--runtime', SLICE,
            'CPUQuota=50.00%', 'CPUQuotaPeriodSec=100000us',
            'MemoryMax=%d' % (256 * 1024 * 1024), 'MemorySwapMax=0')

    def test_a_ceiling_systemd_refuses_fails_closed(self):
        self.execute.side_effect = exception.CommandError(
            cmd='systemctl', error='Failed to connect to bus')

        self.assertRaises(exception.ZunException,
                          self.driver._apply_pod_ceiling,
                          self.capsule, 'runc')

    @mock.patch.object(cri_driver.CriDriver, '_io_device',
                       return_value='253:0')
    def test_block_io_limits_are_properties_too(self, _device):
        self.capsule.blkio_weight = 500
        self.capsule.device_read_bps = 1048576
        self.capsule.device_write_bps = None
        self.capsule.device_read_iops = None
        self.capsule.device_write_iops = 200

        self.driver._apply_host_io_limits(self.capsule, 'sandbox-id')

        self.execute.assert_called_once_with(
            'systemctl', 'set-property', '--runtime', SLICE,
            'IOReadBandwidthMax=/dev/block/253:0 1048576',
            'IOWeight=%d' % hostio.io_weight(500),
            'IOWriteIOPSMax=/dev/block/253:0 200')

    def test_the_slice_is_stopped_and_its_properties_dropped(self):
        self.driver._release_slice(self.capsule)

        self.assertEqual([mock.call('systemctl', 'stop', SLICE),
                          mock.call('systemctl', 'revert', SLICE)],
                         self.execute.call_args_list)

    def test_a_dashed_parent_nests_as_systemd_reads_it(self):
        self.config(cri_sandbox_cgroup_parent='machine-zun.slice')

        self.assertEqual(
            'machine-zun-ea8e2a25_2901_438d_8157_de7ffd68d051.slice',
            self.driver._slice_name(self.capsule))
