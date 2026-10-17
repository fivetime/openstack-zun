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

Three kinds of object, three event prefixes, so a consumer can subscribe
to and account for each on its own and never count one twice:

- a container: ``container.*``
- a capsule: ``capsule.*`` -- its allocation follows k8s (the larger of its
  containers' sum and each init container), so it is not the sum of its
  children; the payload names the children and says how it was worked out;
- a capsule's container (init containers included): ``capsule.container.*``
  -- names its capsule and says its allocation is counted there.

Every payload carries ``occurred_at``, when the thing happened, so a
consumer timestamps a sample with it rather than with when the message
arrived; a message delivered twice then says the same thing twice. Stop
and delete carry the last reading taken before the work, so a container
gone between two polls still accounts for what it used.

Nothing here is built when no notification driver is configured.
"""

import contextlib
import datetime

from oslo_log import log as logging
from oslo_utils import timeutils

from zun.common import consts
from zun.common import rpc

LOG = logging.getLogger(__name__)

SERVICE = 'zun'


PAYLOAD_VERSION = '2.0'

_KIND = {
    consts.TYPE_CONTAINER: 'container',
    consts.TYPE_CAPSULE: 'capsule',
    consts.TYPE_CAPSULE_CONTAINER: 'capsule_container',
    consts.TYPE_CAPSULE_INIT_CONTAINER: 'capsule_init_container',
}

_PREFIX = {
    'container': 'container',
    'capsule': 'capsule',
    'capsule_container': 'capsule.container',
    'capsule_init_container': 'capsule.container',
}

#: How a capsule's allocation is worked out, carried with it so a reader
#: knows it cannot be rebuilt by adding up the children.
CAPSULE_ALLOCATION_BASIS = 'max(sum(containers), max(init_containers))'


def enabled():
    """Whether anything would be sent: a caller skips reading for it."""
    return rpc.get_notifier(service=SERVICE) is not None


def _stamp(value):
    """A record's time as ISO 8601 UTC."""
    if not value:
        return None
    if not hasattr(value, 'isoformat'):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.isoformat()


def _kind(container):
    return _KIND.get(getattr(container, 'container_type', None), 'container')


def _event_type(container, event, phase=None):
    event_type = '%s.%s' % (_PREFIX[_kind(container)], event)
    return '%s.%s' % (event_type, phase) if phase else event_type


def _children(capsule):
    return list(capsule.containers or []) + list(capsule.init_containers or [])


def _capsule_uuid_of(context, child):
    """The uuid of the capsule a capsule container belongs to, or None."""
    from zun import objects
    try:
        found = objects.Capsule.list(context, limit=1,
                                     filters={'id': child.capsule_id})
    except Exception as exc:                                # noqa: BLE001
        LOG.debug('no capsule for %s: %s', child.uuid, exc)
        return None
    return found[0].uuid if found else None


def _payload(container, capsule_uuid=None, context=None):
    """The fields a meter is built from."""
    kind = _kind(container)
    payload = {
        'payload_version': PAYLOAD_VERSION,
        'container_type': kind,
        'occurred_at': timeutils.utcnow(with_timezone=True).isoformat(),
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
        'runtime': getattr(container, 'runtime', None),
        'created_at': _stamp(container.created_at),
        'started_at': _stamp(container.started_at),
        'labels': container.labels,
    }
    if kind == 'capsule':
        payload['children'] = [c.uuid for c in _children(container)]
        payload['allocation_basis'] = CAPSULE_ALLOCATION_BASIS
    elif kind in ('capsule_container', 'capsule_init_container'):
        if capsule_uuid is None and context is not None:
            capsule_uuid = _capsule_uuid_of(context, container)
        payload['capsule_uuid'] = capsule_uuid
        payload['allocation_included_in_parent'] = True
    return payload


@contextlib.contextmanager
def lifecycle(context, container, event, host=None, extra=None,
              end_extra=None):
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
    notify(context, container, event, 'end', host=host,
           extra=dict(extra or {}, **(end_extra or {})))


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
    payload = _payload(container, context=context)
    # Which fields moved, so a consumer can tell a status change from a
    # resize without diffing; the new values are already in the payload.
    # obj_get_changes carries new values, not old, so no before-state is
    # claimed here.
    payload['changed'] = changed
    try:
        notifier.info(context, _event_type(container, 'update'), payload)
    except Exception as exc:
        LOG.warning('could not send %s for %s: %s',
                    _event_type(container, 'update'), container.uuid, exc)


def notify(context, container, event, phase=None, host=None, extra=None,
           children_extra=None):
    """Send one lifecycle notification.

    Never raises. A container that was created must not be reported as
    having failed because the notification bus was busy, and a deployment
    with nothing listening should not pay for that in errors.

    A capsule's create and delete end also announce each of its
    containers (``capsule.container.*``), which are created and removed
    with it and never on their own; `children_extra` carries per-child
    additions by uuid (their last readings, on delete).
    """
    notifier = rpc.get_notifier(service=SERVICE, host=host)
    if notifier is None:
        return
    _send(context, notifier, container, _event_type(container, event, phase),
          extra=extra)
    if (_kind(container) == 'capsule' and phase == 'end' and
            event in ('create', 'delete')):
        for child in _children(container):
            _send(context, notifier, child,
                  _event_type(child, event, phase),
                  extra=(children_extra or {}).get(child.uuid),
                  capsule_uuid=container.uuid)


def _send(context, notifier, container, event_type, extra=None,
          capsule_uuid=None):
    payload = _payload(container, capsule_uuid=capsule_uuid, context=context)
    if extra:
        payload.update(extra)
    try:
        notifier.info(context, event_type, payload)
    except Exception as exc:
        LOG.warning('could not send %s for %s: %s',
                    event_type, container.uuid, exc)


def notify_error(context, container, event, exc, host=None):
    """The error half of a start/end pair.

    Sent as an error so a consumer can tell a container that failed from
    one that was never attempted, which a missing `.end` alone does not.
    """
    notifier = rpc.get_notifier(service=SERVICE, host=host)
    if notifier is None:
        return
    payload = _payload(container, context=context)
    payload['reason'] = str(exc)
    event_type = _event_type(container, event, 'error')
    try:
        notifier.error(context, event_type, payload)
    except Exception as send_error:
        LOG.warning('could not send %s for %s: %s',
                    event_type, container.uuid, send_error)
