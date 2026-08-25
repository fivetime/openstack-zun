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

"""Telling the rest of the cloud what happened to a container.

zun has kept its lifecycle to itself: the hook for it has been in
wrap_exception since the beginning with nothing passed to it, so ceilometer
has had nothing to meter and every consumer that wanted to know a container
existed has had to poll for it.

The payload carries what a bill is computed from -- how much was asked for
and for how long -- rather than everything known about a container. A
notification is read by things that were not written alongside it, so it
should be small and stable, and anything a consumer needs beyond this can
be fetched with the uuid.
"""

import contextlib

from oslo_log import log as logging
from oslo_utils import timeutils

from zun.common import rpc

LOG = logging.getLogger(__name__)

SERVICE = 'zun'


def _stamp(value):
    return str(value) if value else None


def _payload(container):
    """The fields a meter is built from."""
    return {
        'uuid': container.uuid,
        'name': container.name,
        'user_id': container.user_id,
        'tenant_id': container.project_id,
        'project_id': container.project_id,
        'host': container.host,
        'image': container.image,
        'status': container.status,
        'task_state': container.task_state,
        'cpu': container.cpu,
        'memory': container.memory,
        'disk': container.disk,
        'created_at': _stamp(container.created_at),
        'started_at': _stamp(container.started_at),
        'labels': container.labels,
    }


@contextlib.contextmanager
def lifecycle(context, container, event, host=None, extra=None):
    """Wrap an operation so its notification is a start/end/error whole.

    Entering sends `.start`, leaving cleanly sends `.end`, and leaving by
    exception sends `.error` before the exception continues -- so the
    three phases stay in step and no path can send one without the
    others. The same shape nova gives every action.

    Driver-agnostic on purpose: the container handed in is a Container or
    a Capsule, and neither this nor the caller knows or cares which
    runtime is underneath. The place to describe what happened to a
    container is above the driver, not inside each one.
    """
    notify(context, container, event, 'start', host=host, extra=extra)
    try:
        yield
    except Exception as exc:
        notify_error(context, container, event, exc, host=host)
        raise
    notify(context, container, event, 'end', host=host, extra=extra)


def notify_state_change(context, container, changes):
    """A container's state changed, whoever changed it.

    Sent from save(), so it fires for a status a compute node synced back
    after the container stopped on its own as readily as for one an
    operation set -- which is the event a bill needs and the lifecycle
    pairs alone do not carry: a container that exited by itself sends no
    `.end` for anything. nova's instance.update.

    `changes` is what obj_get_changes returned; a move of status,
    task_state, cpu, memory or disk is worth a message -- the first two
    for the lifecycle a container reaches on its own, the last three
    because a resize changes what it costs. A container that exited by
    itself, or was resized, both come through here without any operation
    having sent a lifecycle pair.
    """
    changed = [k for k in ('status', 'task_state', 'cpu', 'memory', 'disk')
               if k in changes]
    if not changed:
        return
    notifier = rpc.get_notifier(service=SERVICE, host=container.host)
    if notifier is None:
        return
    payload = _payload(container)
    # Which fields moved, so a consumer can tell a status change from a
    # resize without diffing; the new values are already in the payload.
    # obj_get_changes carries new values, not old, so no before-state is
    # claimed here.
    payload['changed'] = changed
    try:
        notifier.info(context, 'container.update', payload)
    except Exception as exc:
        LOG.warning('could not send container.update for %s: %s',
                    container.uuid, exc)


def notify(context, container, event, phase=None, host=None, extra=None):
    """Send one lifecycle notification.

    Never raises. A container that was created must not be reported as
    having failed because the notification bus was busy, and a deployment
    with nothing listening should not pay for that in errors.
    """
    notifier = rpc.get_notifier(service=SERVICE, host=host)
    if notifier is None:
        return
    event_type = 'container.%s' % event
    if phase:
        event_type = '%s.%s' % (event_type, phase)
    payload = _payload(container)
    if extra:
        payload.update(extra)
    try:
        notifier.info(context, event_type, payload)
    except Exception as exc:
        LOG.warning('could not send %s for %s: %s',
                    event_type, container.uuid, exc)


def notify_exists(context, container, size_rw=None, window_start=None,
                  host=None):
    """One container, and that it was there for this period. Billing.

    Its own message per container rather than a share of a per-host one,
    because a meter is made of a container: the fields a bill is rated
    from -- who owns it, what it was given, how long the period ran --
    have to travel together for one container, and a consumer that has to
    take them apart from a batch cannot name the meter it is building.
    nova's instance.exists, at the same cadence and for the same reason.

    Distinct from container.usage, which is batched per host on a much
    shorter clock and feeds a cache rather than a bill.
    """
    notifier = rpc.get_notifier(service=SERVICE, host=host or container.host)
    if notifier is None:
        return
    now = timeutils.utcnow()
    payload = _payload(container)
    payload.update({
        'audit_period_beginning': (window_start or now).isoformat(),
        'audit_period_ending': now.isoformat(),
        'size_rw': size_rw,
    })
    try:
        notifier.info(context, 'container.exists', payload)
    except Exception as exc:
        LOG.warning('could not send container.exists for %s: %s',
                    container.uuid, exc)


def notify_usage(context, host, containers, sizes, window_start=None):
    """One report per host: what its containers use, and that they exist.

    Two things a bill needs, in one message a minute rather than two.
    Each entry carries the writable-layer bytes (unknown until a node has
    measured it) and the container's status, so a consumer learns both
    what was used and that the container was there in this window to use
    it -- nova's usage-exists audit, at the granularity zun already pays
    for by reporting at all.

    The window is [previous report, now]: consecutive reports abut, so a
    consumer can sum them into a billing period without gaps or overlap.
    """
    notifier = rpc.get_notifier(service=SERVICE, host=host)
    if notifier is None:
        return
    now = timeutils.utcnow()
    payload = {
        'host': host,
        'measured_at': now.isoformat(),
        'audit_period_beginning': (window_start or now).isoformat(),
        'audit_period_ending': now.isoformat(),
        'containers': [{'uuid': c.uuid,
                        'size_rw': sizes.get(c.uuid),
                        'status': c.status,
                        'cpu': c.cpu,
                        'memory': c.memory}
                       for c in containers],
    }
    try:
        notifier.info(context, 'container.usage', payload)
    except Exception as exc:
        LOG.warning('could not send container.usage for %s: %s', host, exc)


def notify_error(context, container, event, exc, host=None):
    """The error half of a start/end pair.

    Sent as an error so a consumer can tell a container that failed from
    one that was never attempted, which a missing `.end` alone does not.
    """
    notifier = rpc.get_notifier(service=SERVICE, host=host)
    if notifier is None:
        return
    payload = _payload(container)
    payload['reason'] = str(exc)
    try:
        notifier.error(context, 'container.%s.error' % event, payload)
    except Exception as send_error:
        LOG.warning('could not send container.%s.error for %s: %s',
                    event, container.uuid, send_error)
