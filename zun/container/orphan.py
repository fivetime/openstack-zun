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

"""Reaping runtime objects nothing claims any more.

Both driver paths leak the same way and for the same reason: the runtime
outlives the record of why it was started. A create that fails after the
sandbox is up, a delete that dies between killing the task and forgetting
it, a data root wiped from under a running daemon -- each leaves a task
running that no longer belongs to anything, holding memory, a VM, an OVS
port, a mapped device.

What differs between the paths is only who gets to say a thing is claimed.
On the Docker path the authority is dockerd: if it does not know the id, no
container of ours is behind it. On the CRI path it is Kubernetes, which is
not reachable from here -- kubezun holds that answer -- so the adapter there
is deliberately narrower and off by default.

Everything else is common, and it is the part that is easy to get
dangerously wrong: never leave the namespace we were given, never touch
what is too young to be anything but a create in flight, and say out loud
what was removed.
"""

from oslo_log import log as logging

LOG = logging.getLogger(__name__)


class RuntimeObject(object):
    """One thing the runtime is holding, as the sweep needs to see it."""

    def __init__(self, ident, age_seconds, label=None):
        self.ident = ident
        self.age_seconds = age_seconds
        self.label = label or ident

    def __repr__(self):
        return '<RuntimeObject %s age=%ss>' % (self.ident, self.age_seconds)


def sweep(namespace, inventory, is_claimed, remove, min_age,
          dry_run=False):
    """Remove runtime objects in one namespace that nothing claims.

    :param namespace: runtime namespace being swept, for logs and for the
        caller's own sanity -- an adapter that can see more than one must
        have filtered already.
    :param inventory: iterable of RuntimeObject in that namespace.
    :param is_claimed: callable(RuntimeObject) -> bool, the authority. It
        answers "does something that should exist still own this?" and it
        is the only part that differs per driver.
    :param remove: callable(RuntimeObject), removes it from the runtime.
    :param min_age: seconds an object must have existed before it may be
        called an orphan. Below it, a create in flight and a leak look
        exactly alike, and guessing wrong destroys a container somebody is
        still waiting for.
    :param dry_run: log what would go, remove nothing.
    :returns: (reaped, skipped_young, failed)
    """
    reaped = skipped_young = failed = 0

    for obj in inventory:
        try:
            if is_claimed(obj):
                continue
        except Exception as e:
            # An authority that cannot answer is not permission to delete.
            LOG.warning('[%(ns)s] cannot decide whether %(id)s is claimed, '
                        'leaving it: %(err)s',
                        {'ns': namespace, 'id': obj.label, 'err': e})
            failed += 1
            continue

        if obj.age_seconds is None or obj.age_seconds < min_age:
            LOG.debug('[%(ns)s] %(id)s is unclaimed but only %(age)ss old, '
                      'leaving it', {'ns': namespace, 'id': obj.label,
                                     'age': obj.age_seconds})
            skipped_young += 1
            continue

        if dry_run:
            LOG.info('[%(ns)s] would reap %(id)s (age %(age)ss)',
                     {'ns': namespace, 'id': obj.label,
                      'age': obj.age_seconds})
            reaped += 1
            continue

        try:
            remove(obj)
            LOG.info('[%(ns)s] reaped orphan %(id)s (age %(age)ss)',
                     {'ns': namespace, 'id': obj.label,
                      'age': obj.age_seconds})
            reaped += 1
        except Exception as e:
            LOG.warning('[%(ns)s] could not reap %(id)s: %(err)s',
                        {'ns': namespace, 'id': obj.label, 'err': e})
            failed += 1

    if reaped or failed:
        LOG.info('[%(ns)s] orphan sweep: %(r)s reaped, %(y)s too young, '
                 '%(f)s failed', {'ns': namespace, 'r': reaped,
                                  'y': skipped_young, 'f': failed})
    return reaped, skipped_young, failed
