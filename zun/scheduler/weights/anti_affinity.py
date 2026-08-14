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

"""Spread capsules that share an owner across hosts."""

from oslo_log import log as logging

from zun import objects

LOG = logging.getLogger(__name__)

# The label kubezun stamps on every capsule with the UID of the workload that
# owns the pod (its controller ownerReference: the StatefulSet, the
# ReplicaSet). ⚠️ Not the pod name and not the pod UID: keeper-0 and keeper-1
# have different names and different UIDs, and the whole point is that they
# are the same thing three times.
OWNER_LABEL = 'knaas.io/owner-uid'


class OwnerAntiAffinityWeigher(object):
    """Prefer the host with the fewest capsules of the same owner.

    Weight is the negated count, so an empty host weighs 0 and every
    same-owner capsule already present costs one. No owner label means no
    opinion: every host weighs 0 and the filter order stands, which keeps
    capsules made outside Kubernetes (the Horizon tier) unaffected.
    """

    def weigh(self, context, host_state, container):
        owner = (container.labels or {}).get(OWNER_LABEL)
        if not owner:
            return 0.0
        counts = _owner_counts(context, owner)
        return -float(counts.get(host_state.hostname, 0))


def _owner_counts(context, owner):
    """How many capsules of this owner each host runs.

    One listing per scheduling decision, not per host: weigh() is called once
    per candidate host, and listing inside it would turn one decision into
    N identical queries. The cache lives on the context, which exists for
    exactly one select_destinations call.
    """
    cached = getattr(context, '_knaas_owner_counts', None)
    if cached is not None:
        return cached
    counts = {}
    try:
        # ⚠️ Capsule.list, not Container.list. Capsules are Container-table
        # rows of their own type, and the template's labels — including the
        # owner — live on the capsule row. Container.list answers with other
        # row types whose labels are empty, which makes every count zero and
        # the weigher silently neutral: measured, that is exactly how the
        # first deployment of this file changed nothing.
        for c in objects.Capsule.list(context):
            if not c.host:
                continue
            if (c.labels or {}).get(OWNER_LABEL) == owner:
                counts[c.host] = counts.get(c.host, 0) + 1
    except Exception as e:
        # No count is a neutral answer, not a wrong one: the scheduler falls
        # back to filter order, which is where it stood before this existed.
        LOG.warning('Owner anti-affinity could not count: %s', e)
    context._knaas_owner_counts = counts
    return counts
