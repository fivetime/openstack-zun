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

from zun.container.cri import resources as driver
from zun.tests import base


class LinuxResourcesTest(base.BaseTestCase):
    """The CRI resource block mirrors what the kubelet sends.

    kubezun hands this driver the pod's LIMITS, and a tenant reads them as
    ceilings. cpu_shares alone is a weight -- on an idle host it lets a
    container run on every core -- so the ceiling has to be quota over a
    period, and the memory ceiling has to close the swap door the runtime
    would otherwise leave at its own default.
    """

    def test_cpu_limit_becomes_a_quota_not_only_a_share(self):
        res = driver.linux_resources(cpu=1.5, memory_mb=None)
        self.assertEqual(1536, res['cpu_shares'])
        # 1.5 CPU over a 100ms period = 150ms of CPU per period.
        self.assertEqual(150000, res['cpu_quota'])
        self.assertEqual(100000, res['cpu_period'])

    def test_memory_limit_also_caps_swap_at_the_same_value(self):
        res = driver.linux_resources(cpu=None, memory_mb=256)
        self.assertEqual(256 * 1024 * 1024, res['memory_limit_in_bytes'])
        # kubelet default: swap off, expressed as swap limit == memory limit.
        self.assertEqual(res['memory_limit_in_bytes'],
                         res['memory_swap_limit_in_bytes'])

    def test_no_limit_sends_no_quota_not_a_zero_quota(self):
        # ⚠️ The dangerous inversion: quota=0 means "no cpu at all" to the
        # kernel, so an unset limit must send NO quota key rather than 0.
        res = driver.linux_resources(cpu=None, memory_mb=None)
        self.assertEqual({}, res)
        res = driver.linux_resources(cpu=0, memory_mb=0)
        self.assertNotIn('cpu_quota', res)
        self.assertNotIn('memory_limit_in_bytes', res)

    def test_fractional_cpu_rounds_to_whole_microseconds(self):
        res = driver.linux_resources(cpu=0.5, memory_mb=None)
        self.assertEqual(50000, res['cpu_quota'])
        self.assertIsInstance(res['cpu_quota'], int)

    def test_string_values_from_the_raw_api_are_normalised(self):
        # A capsule created straight through the Zun API carries cpu/memory as
        # strings; kubezun sends numbers. Both must yield the same block.
        res = driver.linux_resources(cpu="0.5", memory_mb="128")
        self.assertEqual(50000, res['cpu_quota'])
        self.assertEqual(128 * 1024 * 1024, res['memory_limit_in_bytes'])
        # Unparseable reads as unset, not as a crash that kills the create.
        self.assertEqual({}, driver.linux_resources(cpu="", memory_mb=None))


class SandboxResourcesTest(base.BaseTestCase):
    """The pod-level ceiling handed to the sandbox, kubelet's arithmetic.

    A sandboxed runtime holds every container in one host process, so the
    per-container blocks land in cgroups holding no processes; this block is
    how the runtime learns the pod's ceiling at all. Measured before it was
    sent: a runsc sandbox for a 1-cpu/256Mi capsule booted with the host's
    own 16 cpus and 31G.
    """

    def test_members_sum_into_the_ceiling(self):
        res = driver.sandbox_resources([(1, 256), (0.5, 128)])
        self.assertEqual(150000, res['cpu_quota'])
        self.assertEqual(100000, res['cpu_period'])
        self.assertEqual(int(1024 * 1.5), res['cpu_shares'])
        self.assertEqual(384 * 1024 * 1024, res['memory_limit_in_bytes'])
        # Swap off, the kubelet default: the total is the memory limit.
        self.assertEqual(384 * 1024 * 1024,
                         res['memory_swap_limit_in_bytes'])

    def test_one_unlimited_member_lifts_that_ceiling(self):
        # kubelet's own rule: summing the rest anyway would squeeze the
        # unlimited member under a number nobody asked for.
        res = driver.sandbox_resources([(1, 256), (None, 128)])
        self.assertNotIn('cpu_quota', res)
        # Each resource is judged on its own: memory is limited on every
        # member here, so its ceiling stands while the cpu one lifts.
        self.assertEqual(384 * 1024 * 1024, res['memory_limit_in_bytes'])
        # The weight still sums over whoever declared one.
        self.assertEqual(1024, res['cpu_shares'])

        res = driver.sandbox_resources([(1, 256), (0.5, None)])
        self.assertEqual(150000, res['cpu_quota'])
        self.assertNotIn('memory_limit_in_bytes', res)

    def test_nothing_limited_sends_nothing(self):
        self.assertEqual({}, driver.sandbox_resources([]))
        self.assertEqual({}, driver.sandbox_resources([(None, None)]))
        self.assertEqual({}, driver.sandbox_resources([("", "")]))

    def test_raw_api_strings_are_normalised(self):
        res = driver.sandbox_resources([("0.5", "128"), ("0.5", "128")])
        self.assertEqual(100000, res['cpu_quota'])
        self.assertEqual(256 * 1024 * 1024, res['memory_limit_in_bytes'])
