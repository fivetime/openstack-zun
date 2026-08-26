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

"""What "CPU %" means, and that both drivers mean the same by it.

docker's scale: 100% is one CPU fully used, so four busy cores read 400%.
The two drivers reach it from different counters -- one has the machine's
total CPU time, the other a wall clock -- and have to arrive at the same
number, or the same key means different things depending on which runtime
a container happened to land on.
"""

from zun.container import driver
from zun.tests import base

SECOND = 10 ** 9


class CpuPercentTest(base.TestCase):
    """The docker driver's counters: container time over machine time."""

    def test_one_busy_cpu_is_one_hundred_percent(self):
        # A second of wall clock on a 4-CPU host is 4 CPU-seconds of
        # machine time; the container spent one of them.
        self.assertEqual(100.0, driver.cpu_percent(
            SECOND, 0, 4 * SECOND, 0, online_cpus=4))

    def test_every_cpu_busy_reads_as_one_hundred_per_cpu(self):
        self.assertEqual(400.0, driver.cpu_percent(
            4 * SECOND, 0, 4 * SECOND, 0, online_cpus=4))

    def test_an_idle_container_is_zero(self):
        self.assertEqual(0.0, driver.cpu_percent(
            SECOND, SECOND, 2 * SECOND, SECOND, online_cpus=2))

    def test_a_first_reading_has_nothing_to_compare_with(self):
        """Not a share of no elapsed time."""
        self.assertEqual(0.0, driver.cpu_percent(
            SECOND, SECOND, 2 * SECOND, 2 * SECOND, online_cpus=2))

    def test_a_ratio_of_raw_counters_would_not_be_a_percentage(self):
        """The old way: both counters climb from boot, so it tends to zero.

        A container that has just used a whole CPU for a second on a host
        up for a month must not read as approximately nothing.
        """
        month = 30 * 24 * 3600 * SECOND
        honest = driver.cpu_percent(month + SECOND, month,
                                    (month + SECOND) * 4, month * 4,
                                    online_cpus=4)
        self.assertEqual(100.0, honest)


class CpuPercentOverTimeTest(base.TestCase):
    """The CRI driver's counters: container time over a wall clock."""

    def test_one_busy_cpu_is_one_hundred_percent(self):
        self.assertEqual(100.0, driver.cpu_percent_over_time(
            SECOND, 0, SECOND))

    def test_four_busy_cpus_read_four_hundred(self):
        self.assertEqual(400.0, driver.cpu_percent_over_time(
            4 * SECOND, 0, SECOND))

    def test_it_does_not_divide_by_the_hosts_core_count(self):
        """The bug this replaced: a share of the whole machine instead.

        On a 32-core host that reported one busy core as 3.125%.
        """
        self.assertEqual(100.0, driver.cpu_percent_over_time(
            SECOND, 0, SECOND))

    def test_no_elapsed_time_is_not_a_rate(self):
        self.assertEqual(0.0, driver.cpu_percent_over_time(SECOND, 0, 0))


class BothDriversAgreeTest(base.TestCase):
    """The same load has to read the same, whichever runtime reported it."""

    def test_one_busy_cpu_on_a_thirty_two_core_host(self):
        docker_side = driver.cpu_percent(
            SECOND, 0, 32 * SECOND, 0, online_cpus=32)
        cri_side = driver.cpu_percent_over_time(SECOND, 0, SECOND)

        self.assertEqual(docker_side, cri_side)
        self.assertEqual(100.0, docker_side)
