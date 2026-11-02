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

"""Files left on a node by containers that no longer exist.

A file is named after the container it belongs to, and nothing removes it when
the service died before the delete got there, or when the delete never reached
this node: one whose record had lost its host goes no further than the API. The
external lock of a CNI operation has no delete at all, because removing it from
under a waiter would let two hold it at once.

So what a file's name no longer claims is found by looking.
"""

import os
import re
import time

from oslo_log import log as logging

LOG = logging.getLogger(__name__)

_UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

#: A lock is named by the uuid and nothing else; a name that is not one is
#: somebody else's lock.
LOCK = re.compile('^(%s)$' % _UUID)
LOG_FILE = re.compile(r'^(%s)\.log(\.\d+)?$' % _UUID)
HOSTS_FILE = re.compile(r'^(%s)\.hosts$' % _UUID)


def unclaimed(root, pattern, claimed, min_age):
    """Paths under root that pattern names and no claimed uuid accounts for.

    Written to within min_age is left out whatever its name says: something is
    still at work on it, and a create may not have reached the database's
    answer yet.
    """
    found = set()
    now = time.time()
    try:
        entries = os.scandir(root)
    except FileNotFoundError:
        return found
    with entries:
        for entry in entries:
            match = pattern.match(entry.name)
            if not match or match.group(1) in claimed:
                continue
            try:
                if (entry.is_file(follow_symlinks=False) and
                        now - entry.stat().st_mtime >= min_age):
                    found.add(entry.path)
            except FileNotFoundError:
                continue
    return found


def remove(paths):
    """Remove what can be removed, and return how many went."""
    removed = 0
    for path in paths:
        try:
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            LOG.warning('Could not remove %(path)s: %(err)s',
                        {'path': path, 'err': e})
    return removed
