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

"""Host weighers for the filter scheduler.

Upstream Zun has filters only, and a filter is a hard judgement: a host that
does not pass is gone, and an empty result is NoValidHost. What the platform
needs from anti-affinity is SOFT (DESIGN kubezun §4.5): prefer spreading, but
a single-host deployment must still schedule its second replica. A weigher is
an ordering, never a refusal, so the fallback is built into its shape.

This is a seed of Nova's weigher framework, not a port of it: one base class,
a static list of weighers, no config plumbing. The anti-affinity weigher is
platform-default-on because the default it replaces was measured actively
harmful -- first-fit stacked eight capsules, three of them one StatefulSet,
onto one host while two sat empty.

⚠️ Anti-affinity speaks only for capsules carrying an owner label, so for a
long while everything else was still first-fit -- measured again on a
three-node deployment, thirty containers in a row onto one host with two
idle. The spread weigher covers what anti-affinity has no opinion about; the
two are meant to be read together, and a weigher that answers for only some
containers is not a default for all of them.
"""

from zun.scheduler.weights.anti_affinity import OwnerAntiAffinityWeigher
from zun.scheduler.weights.spread import FreeMemoryWeigher


class BaseWeigher(object):
    """One number per host; bigger is better."""

    def weigh(self, context, host_state, container):
        raise NotImplementedError()


def all_weighers():
    return [OwnerAntiAffinityWeigher(), FreeMemoryWeigher()]


def order_hosts(context, hosts, container):
    """Sort hosts best-first by the sum of all weighers.

    ⚠️ Pure ordering. The claim loop after this still tries hosts in turn, so
    a host this ranks last is still used when every better one cannot claim --
    which is exactly the softness a default-on policy needs.

    Stable sort, so hosts the weighers cannot tell apart keep the order the
    filters produced.
    """
    weighers = all_weighers()
    scored = []
    for host in hosts:
        total = 0.0
        for w in weighers:
            total += w.weigh(context, host, container)
        scored.append((total, host))
    scored.sort(key=lambda pair: -pair[0])
    return [host for _, host in scored]
