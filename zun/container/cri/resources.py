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

"""Translate a container's cpu/memory into the CRI linux resource block.

Kept free of the protobuf gencode on purpose: this is pure arithmetic that
must be unit-testable anywhere, and importing the driver drags in a
generated module pinned to a protobuf runtime that not every environment
has.
"""

# One CPU-second per period, the kubelet's own value
# (kubernetes/pkg/kubelet/cm/helpers_linux.go, QuotaPeriod).
_CPU_PERIOD_US = 100000


def linux_resources(cpu, memory_mb):
    """Translate a container's cpu/memory into the CRI resource block.

    Mirrors what the kubelet itself sends
    (pkg/kubelet/kuberuntime/kuberuntime_container_linux.go), because the
    values kubezun hands us are the pod's Kubernetes LIMITS and a tenant
    reads them as ceilings:

    - cpu_shares alone -- what this driver sent before -- is a weight, not a
      ceiling: it only bites when the host is contended, and on an idle
      host a container with `limits.cpu: 1` runs on every core. Adding
      cpu_quota over a fixed period turns the number back into the ceiling
      the spec promised.
    - memory_swap_limit_in_bytes equal to the memory limit is the kubelet's
      default (swap off), so a memory ceiling cannot be quietly widened by
      the runtime's swap default.

    ⚠️ Zero means "not set", and the kubelet's convention is honoured: no
    cpu limit sends no quota (unlimited), not quota=0 (which the kernel
    reads as "no cpu at all").
    """
    # ⚠️ Both arrive as strings when a capsule is created straight through the
    # Zun API (the JSON template carries them verbatim), and as numbers when
    # kubezun builds the template. Normalise before comparing: measured, the
    # first cut compared a str against 0 and every API-created capsule went
    # to Error with "'>' not supported between 'str' and 'int'".
    cpu = _number(cpu)
    memory_mb = _number(memory_mb)
    resources = {}
    if cpu > 0:
        resources['cpu_shares'] = int(1024 * cpu)
        resources['cpu_quota'] = int(cpu * _CPU_PERIOD_US)
        resources['cpu_period'] = _CPU_PERIOD_US
    if memory_mb > 0:
        limit = int(memory_mb) * 1024 * 1024
        resources['memory_limit_in_bytes'] = limit
        resources['memory_swap_limit_in_bytes'] = limit
    return resources


def _number(value):
    """None, '', and unparseable all read as 'not set' (0), never as an error:
    a resource that cannot be read must not stop the container from being
    created -- unlimited is the pre-existing behaviour for an absent value."""
    if value is None or value == '':
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


