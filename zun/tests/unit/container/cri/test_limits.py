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

"""The limits this driver can apply, and the ones it has to turn down.

The runtime interface carries cpu, memory and swap and nothing else:
there is no field for a pids limit or for block IO, and containerd never
sets the OCI Pids block from it. The `unified` map would be the way out
on runc, but the kata agent's cgroup manager does not read it, so on a VM
runtime passing it would be one more silent drop.

Measured before this: a container asking for `--pids-limit 64` ran with
`pids.max = max` and inspect answered null -- a container that believes
it is bounded and is not, which for a limit whose whole job is to bound a
fork bomb is the dangerous half of the mistake.
"""

from unittest import mock

from zun.container.cri import driver as cri_driver
from zun.container.cri import resources
from zun.tests import base


class SwapTest(base.TestCase):
    """The field is a total -- memory plus swap -- in both systems."""

    def test_swap_is_added_to_the_memory_limit(self):
        block = resources.linux_resources(0, 512, 256)

        self.assertEqual(512 * 1024 * 1024, block['memory_limit_in_bytes'])
        self.assertEqual((512 + 256) * 1024 * 1024,
                         block['memory_swap_limit_in_bytes'])

    def test_no_swap_asked_for_means_swap_off(self):
        """limit == memory is the kubelet's own default."""
        block = resources.linux_resources(0, 512)

        self.assertEqual(512 * 1024 * 1024,
                         block['memory_swap_limit_in_bytes'])

    def test_zero_is_the_same_as_not_asking(self):
        block = resources.linux_resources(0, 512, 0)

        self.assertEqual(512 * 1024 * 1024,
                         block['memory_swap_limit_in_bytes'])

    def test_unlimited_passes_through(self):
        """-1 means unlimited in both systems; adding to it would be wrong."""
        block = resources.linux_resources(0, 512, -1)

        self.assertEqual(-1, block['memory_swap_limit_in_bytes'])

    def test_swap_without_a_memory_limit_sets_nothing(self):
        """A total is meaningless without the half it is a total of."""
        self.assertNotIn('memory_swap_limit_in_bytes',
                         resources.linux_resources(0, 0, 256))


class UnenforceableLimitsTest(base.TestCase):

    def _asked(self, **fields):
        container = mock.Mock(**{f: None for f, _o
                                 in cri_driver.CriDriver.UNENFORCEABLE_LIMITS})
        for k, v in fields.items():
            setattr(container, k, v)
        driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        return driver.unenforceable_limits(container)

    def test_a_pids_limit_is_named(self):
        self.assertEqual([('pids_limit', '--pids-limit')],
                         self._asked(pids_limit=64))

    def test_block_io_weight_is_named(self):
        self.assertEqual([('blkio_weight', '--blkio-weight')],
                         self._asked(blkio_weight=500))

    def test_per_device_throughput_is_named(self):
        self.assertEqual([('device_read_bps', '--device-read-bps')],
                         self._asked(device_read_bps=1048576))

    def test_everything_asked_for_is_named_at_once(self):
        """One refusal listing all of them, not one error per round trip."""
        asked = self._asked(pids_limit=64, blkio_weight=500)

        self.assertEqual([('pids_limit', '--pids-limit'),
                          ('blkio_weight', '--blkio-weight')], asked)

    def test_a_container_asking_for_none_of_them_passes(self):
        self.assertEqual([], self._asked())

    def test_cpu_memory_and_swap_are_not_in_the_list(self):
        """Those this driver does apply, so they are never turned down."""
        fields = [f for f, _o in cri_driver.CriDriver.UNENFORCEABLE_LIMITS]

        for applied in ('cpu', 'memory', 'swap', 'disk'):
            self.assertNotIn(applied, fields)
