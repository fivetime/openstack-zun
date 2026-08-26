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

"""Hearing what compute nodes report, and keeping the latest of it.

Every API replica runs one of these and they all join the same consumer
group, so the bus hands each report to exactly one of them. That is the
whole of the coordination: no replica is elected, none is special, and one
going away just moves its share of the partitions to the others.
"""

from oslo_log import log as logging
import oslo_messaging as messaging

from zun.common import rpc
from zun.common import usage_cache
import zun.conf

CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)


class UsageEndpoint(object):
    """Only container.usage; everything else on the topic is ignored."""

    filter_rule = messaging.NotificationFilter(
        event_type='^container\\.usage$')

    def info(self, ctxt, publisher_id, event_type, payload, metadata):
        host = payload.get('host')
        measured_at = payload.get('measured_at')
        entries = payload.get('containers') or []
        for entry in entries:
            uuid = entry.get('uuid')
            if not uuid:
                continue
            # The reading before this one is kept beside it. A rate is
            # the difference between two readings, and a node sends one
            # at a time, so if the pair is not assembled here nobody can
            # assemble it: a reader gets whichever single reading it
            # happened to ask during, and no two of them are its own.
            held = usage_cache.recall(uuid) or {}
            sampled = entry.get('counters')
            usage_cache.remember(uuid, {
                'size_rw': entry.get('size_rw'),
                'measured_at': measured_at,
                'host': host,
                # Absent when the node could not sample this one; the
                # reader tells that apart from a container using nothing.
                'counters': sampled,
                'previous_counters': (held.get('counters')
                                      if sampled else
                                      held.get('previous_counters')),
                'previous_measured_at': (held.get('measured_at')
                                         if sampled else
                                         held.get('previous_measured_at')),
            })
        LOG.debug('kept usage for %d containers from %s', len(entries), host)
        return messaging.NotificationResult.HANDLED


def start():
    """Begin listening. Returns the listener so a caller can stop it.

    Returns None where there is no notification transport, which is a
    deployment that never configured one: the field then reads unknown
    everywhere, which is the truth about it.
    """
    if rpc.NOTIFICATION_TRANSPORT is None:
        LOG.warning('no notification transport; container usage will '
                    'read as unknown')
        return None
    if not CONF.cache.enabled:
        # The listener lives in the launcher's parent process and the WSGI
        # workers are forked before it starts, so a process-local cache is
        # one the workers can never see: every figure would read unknown
        # although every report was heard. Only a shared backend works.
        LOG.warning('[cache] is not enabled; the usage this listener hears '
                    'is kept in a process-local cache the API workers do '
                    'not share, so size_rw will read as unknown. Point '
                    '[cache] at memcached.')
    targets = [messaging.Target(topic=topic)
               for topic in CONF.oslo_messaging_notifications.topics]
    listener = messaging.get_notification_listener(
        rpc.NOTIFICATION_TRANSPORT, targets, [UsageEndpoint()],
        executor='threading', pool=CONF.usage_report.listener_pool)
    listener.start()
    LOG.info('listening for container usage as %s',
             CONF.usage_report.listener_pool)
    return listener
