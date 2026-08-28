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

"""Prefer the host with the most room left."""


class FreeMemoryWeigher(object):
    """Order hosts by the share of their memory that is still free.

    Without this, hosts that no weigher has an opinion about keep the
    order the filters produced, and that order is stable: the same host
    wins every time and the others stay empty. Measured on a three-node
    deployment, where thirty containers in a row went to one host while
    two sat idle with identical runtimes and no filter excluding them.

    The anti-affinity weigher only speaks for capsules carrying an owner
    label, so everything else -- every container made through the
    Container API -- was first-fit.

    A fraction rather than a byte count, for two reasons. Hosts of
    different sizes compare fairly: the emptiest host wins rather than
    the biggest one. And the value stays inside [0, 1], below the
    integer steps anti-affinity works in, so this breaks ties among
    hosts anti-affinity ranks equally instead of overruling it.
    """

    def weigh(self, context, host_state, container):
        total = float(getattr(host_state, 'mem_total', 0) or 0)
        if total <= 0:
            # Nothing reported yet. No opinion beats a wrong one: a
            # host that looks empty because it has not spoken would
            # otherwise be preferred over every host that has.
            return 0.0
        free = float(getattr(host_state, 'mem_free', 0) or 0)
        return max(0.0, min(1.0, free / total))
