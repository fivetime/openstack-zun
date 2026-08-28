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

"""Where a container goes when no weigher has an opinion about it.

Hosts nothing ranks keep the order the filters produced, and that order
is stable -- so the same host wins every time. Measured on a three-node
deployment: thirty containers in a row onto one host, two idle, same
runtimes, no filter excluding them. The anti-affinity weigher speaks
only for capsules carrying an owner label, which no container made
through the Container API has.
"""

from unittest import mock

from zun.scheduler import weights
from zun.scheduler.weights import spread
from zun.tests import base


def _host(name, total=1000, free=1000):
    return mock.Mock(hostname=name, mem_total=total, mem_free=free)


def _container(labels=None):
    return mock.Mock(labels=labels or {})


class TestTheEmptiestHostIsPreferred(base.TestCase):

    def setUp(self):
        super(TestTheEmptiestHostIsPreferred, self).setUp()
        self.weigher = spread.FreeMemoryWeigher()

    def _weigh(self, host):
        return self.weigher.weigh(mock.Mock(), host, _container())

    def test_a_host_with_more_free_memory_weighs_more(self):
        self.assertGreater(self._weigh(_host('a', free=900)),
                           self._weigh(_host('b', free=100)))

    def test_it_compares_hosts_of_different_sizes_fairly(self):
        """Half of a large host is not emptier than nearly all of a small one.

        A byte count would rank the big machine first however full it
        is, which is how one host ends up carrying everything.
        """
        big_half_used = _host('big', total=64000, free=32000)
        small_nearly_empty = _host('small', total=8000, free=7600)

        self.assertGreater(self._weigh(small_nearly_empty),
                           self._weigh(big_half_used))

    def test_a_host_that_has_not_reported_gets_no_advantage(self):
        """Zero total reads as empty, and empty would win everything."""
        self.assertEqual(0.0, self._weigh(_host('quiet', total=0, free=0)))

    def test_the_weight_stays_within_one(self):
        for free in (0, 500, 1000, 5000):
            weight = self._weigh(_host('h', total=1000, free=free))
            self.assertGreaterEqual(weight, 0.0)
            self.assertLessEqual(weight, 1.0)


class TestItDoesNotOverruleAntiAffinity(base.TestCase):
    """Spreading breaks ties; it does not outrank keeping replicas apart.

    Anti-affinity works in whole containers -- one already here costs
    one -- and this weigher never reaches one, so a host holding a
    replica cannot win on having more memory free.
    """

    def test_a_host_holding_a_replica_loses_to_an_emptier_one(self):
        held = _host('held', free=1000)
        other = _host('other', free=10)
        container = _container({'io.knaas.owner': 'team'})

        with mock.patch.object(weights.OwnerAntiAffinityWeigher, 'weigh',
                               side_effect=lambda _c, h, _n:
                               -1.0 if h.hostname == 'held' else 0.0):
            ordered = weights.order_hosts(mock.Mock(), [held, other],
                                          container)

        self.assertEqual(['other', 'held'], [h.hostname for h in ordered])

    def test_hosts_anti_affinity_ranks_equally_are_split_by_room(self):
        full = _host('full', free=10)
        empty = _host('empty', free=990)

        ordered = weights.order_hosts(mock.Mock(), [full, empty],
                                      _container())

        self.assertEqual(['empty', 'full'], [h.hostname for h in ordered])


class TestItIsOnByDefault(base.TestCase):

    def test_the_weigher_is_in_the_list(self):
        self.assertTrue(any(isinstance(w, spread.FreeMemoryWeigher)
                            for w in weights.all_weighers()))
