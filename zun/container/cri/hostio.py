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

"""The IO limits the runtime interface has no field for, applied on the host.

LinuxContainerResources carries cpu, memory and swap and nothing else, so a
block IO limit cannot be asked for through the CRI at all. It still has to
exist: a container's reads and writes leave the guest through the VMM and
land on the node's disk, which is the thing the node's other tenants share.
Limiting inside the guest would bound the guest's view of its own virtual
disk, which is not what anyone else is competing for.

So it is written where the VMM runs -- the sandbox's own cgroup on the host,
which the runtime has already created by the time this is called. Reaching
past the CRI is a deliberate exception, kept to what the CRI does not serve
(FORK.md 4.3.2).

Kept free of the driver and of the protobuf gencode: the arithmetic and the
file format are what get a limit wrong, and they should be testable without
a runtime.
"""

import os

CGROUP_ROOT = '/sys/fs/cgroup'

#: The controller that has to be enabled down the chain before a cgroup has
#: io.max and io.weight at all. containerd creates the parents with only the
#: controllers it sets itself (cpuset, cpu), so without this the files are
#: simply absent -- measured: a sandbox cgroup with io.pressure and nothing
#: else, and every write refused because the file was not there.
IO_CONTROLLER = 'io'

#: The four caps, in the order they are written, paired with the field each
#: comes from. `max` is the kernel's word for "no limit".
IO_MAX_FIELDS = (
    ('rbps', 'device_read_bps'),
    ('wbps', 'device_write_bps'),
    ('riops', 'device_read_iops'),
    ('wiops', 'device_write_iops'),
)


def io_weight(blkio_weight):
    """docker's 10..1000 as cgroup v2's 1..10000.

    The same conversion runc does, so a weight means the same thing on a
    node running either driver.
    """
    if not blkio_weight:
        return None
    return 1 + (int(blkio_weight) - 10) * 9999 // 990


def io_max_line(device, container):
    """The io.max line for a container's caps, or None if it asks for none.

    ⚠️ `device` is the whole disk, never a partition: the io controller
    matches bios at the disk level and a partition's dev_t silently matches
    nothing. Measured: writing `253:2` (a partition) is accepted by the file
    and throttles nothing; `253:0` (the disk) reads back as written.
    """
    caps = [(key, getattr(container, field, None))
            for key, field in IO_MAX_FIELDS]
    if not any(value for _key, value in caps):
        return None
    parts = ['%s=%s' % (key, int(value) if value else 'max')
             for key, value in caps]
    return '%s %s' % (device, ' '.join(parts))


def io_max_reset(device):
    """The line that takes every cap off again."""
    return '%s %s' % (device, ' '.join('%s=max' % key
                                       for key, _field in IO_MAX_FIELDS))


def enable_io_controller(cgroup_path, root=CGROUP_ROOT):
    """Enable the io controller for every level above `cgroup_path`.

    A cgroup only has the interface files for controllers its *parent*
    enabled in cgroup.subtree_control, and containerd enables only what it
    sets itself. Walking down from the root and enabling io at each level is
    what makes io.max and io.weight appear in the leaf -- including in a
    leaf that already exists and already holds the VMM, which is the case
    here because the sandbox is running by the time we get to it.

    Returns the levels that were changed, for the log.
    """
    relative = cgroup_path.strip('/')
    if not relative:
        return []
    changed = []
    # The root first: its subtree_control is what gives the level below it
    # the io files, without which that level cannot enable io for its own
    # children either. Then every level except the leaf -- a cgroup's own
    # setting governs its children, so the leaf's is irrelevant here.
    levels = [root]
    walked = root
    for element in relative.split('/')[:-1]:
        walked = os.path.join(walked, element)
        levels.append(walked)
    for walked in levels:
        control = os.path.join(walked, 'cgroup.subtree_control')
        try:
            with open(control) as handle:
                enabled = handle.read().split()
            if IO_CONTROLLER in enabled:
                continue
            with open(control, 'w') as handle:
                handle.write('+%s' % IO_CONTROLLER)
        except OSError:
            # Left to the caller: a level that cannot be prepared means the
            # leaf will not have the files, which is a refusal to make where
            # the caller can still make one.
            raise
        changed.append(walked)
    return changed


def has_io_controller(root=CGROUP_ROOT):
    """Whether this host offers the io controller at all."""
    try:
        with open(os.path.join(root, 'cgroup.controllers')) as handle:
            return IO_CONTROLLER in handle.read().split()
    except OSError:
        return False
