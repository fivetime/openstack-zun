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


def notify_usage(context, host, sizes):
    """One report of what a host's containers are using.

    Its own event rather than a field on the lifecycle ones: a consumer
    of container.create.end reads it to know a container exists, and
    would not want it re-sent every minute with a size attached. This
    one carries the uuid, the host and the bytes, and a consumer that
    wants more can fetch it.
    """
    notifier = rpc.get_notifier(service=SERVICE, host=host)
    if notifier is None:
        return
    payload = {
        'host': host,
        'measured_at': timeutils.utcnow().isoformat(),
        'containers': [{'uuid': uuid, 'size_rw': size}
                       for uuid, size in sizes.items()],
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
