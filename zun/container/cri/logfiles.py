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

"""The files a capsule container's output is written to.

Nothing else owns them. The runtime writes the path it was told and never
removes or rotates it -- on a Kubernetes node the kubelet does both, and here
there is no kubelet -- so unless this does, what a tenant printed stays on the
node after the tenant's container is gone.

One container is ``<uuid>.log``, the file being written, and ``<uuid>.log.N``,
older the higher N is.
"""

import os
import re

from oslo_log import log as logging

LOG = logging.getLogger(__name__)

_CURRENT = re.compile(r'^(.+)\.log$')


def current(root, uuid):
    return os.path.join(root, '%s.log' % uuid)


def _rotated(root, uuid, number):
    return '%s.%d' % (current(root, uuid), number)


def _numbers(root, uuid):
    prefix = '%s.log.' % uuid
    found = []
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return found
    for name in names:
        if name.startswith(prefix) and name[len(prefix):].isdigit():
            found.append(int(name[len(prefix):]))
    return sorted(found)


def oldest_first(root, uuid):
    """Every file this container's output is in, in the order it was written.

    A file that is not there is left out: a container that has not started
    has none, and that is not an error.
    """
    paths = [_rotated(root, uuid, n)
             for n in reversed(_numbers(root, uuid))]
    if os.path.exists(current(root, uuid)):
        paths.append(current(root, uuid))
    return paths


def uuid_of(root, path):
    """The container a current log file belongs to, or None."""
    # Normalised: the runtime joins the directory it was given with a file
    # name, so a trailing slash in the option would otherwise match nothing and
    # no log would ever rotate, with nothing to say so.
    if not path or os.path.dirname(path) != os.path.normpath(root):
        return None
    match = _CURRENT.match(os.path.basename(path))
    return match.group(1) if match else None


def oversized(root, limit):
    """The uuids whose file being written has reached limit bytes."""
    found = set()
    try:
        entries = os.scandir(root)
    except FileNotFoundError:
        return found
    with entries:
        for entry in entries:
            match = _CURRENT.match(entry.name)
            if not match:
                continue
            try:
                if entry.stat().st_size >= limit:
                    found.add(match.group(1))
            except FileNotFoundError:
                # Removed by a delete between the listing and the look.
                continue
    return found


def push(root, uuid, keep):
    """Move the current file aside, keeping keep files counting the new one.

    The writer holds the file open and follows it through the rename, so
    nothing is lost between this and the runtime reopening the path.
    """
    numbers = _numbers(root, uuid)
    for number in reversed(numbers):
        if number + 1 >= keep:
            os.unlink(_rotated(root, uuid, number))
        else:
            os.rename(_rotated(root, uuid, number),
                      _rotated(root, uuid, number + 1))
    os.rename(current(root, uuid), _rotated(root, uuid, 1))


def pop(root, uuid):
    """Undo push when the runtime would not reopen: it still writes there."""
    if not os.path.exists(current(root, uuid)):
        os.rename(_rotated(root, uuid, 1), current(root, uuid))


def remove(root, uuid):
    """Remove everything a container printed, never failing the caller.

    Called from a delete, whose purpose is the container and which has already
    succeeded by now: a file that will not go is worth a warning, not a record
    stuck in Deleting.
    """
    for path in oldest_first(root, uuid):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            LOG.warning('Could not remove log file %(path)s: %(err)s',
                        {'path': path, 'err': e})
