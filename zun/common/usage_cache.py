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

"""The latest usage figure heard for each container.

A measurement, not state: it lives in a cache with a lifetime of a few
report intervals and is never written to the database. A figure that is
lost is replaced by the next report; a figure that has aged out reads as
unknown, which is right for a node that has stopped reporting.

[cache] has to point at a shared backend such as memcached. The listener
that fills this runs in the launcher's parent process and the WSGI workers
that read it are forked before it starts, so a process-local dictionary
-- the fallback nova uses for its own caches -- is one the readers can
never see. It is kept as a fallback so the code runs without a cache
configured, but every figure then reads unknown, and the listener says
so at start.
"""

from oslo_cache import core as cache
from oslo_log import log as logging

import zun.conf

CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)

_REGION = None
_PREFIX = 'zun.usage.'


def _region():
    global _REGION
    if _REGION is None:
        region = cache.create_region()
        # The lifetime is set here rather than through [cache]
        # expiration_time so that an operator tuning the report interval
        # does not also have to remember to tune the cache to match.
        CONF.set_override(
            'expiration_time',
            (CONF.usage_report.report_interval
             * CONF.usage_report.retain_reports),
            group='cache')
        if CONF.cache.enabled:
            cache.configure_cache_region(CONF, region)
        else:
            region.configure('dogpile.cache.memory',
                             expiration_time=CONF.cache.expiration_time)
        _REGION = region
    return _REGION


def remember(uuid, figure):
    """Keep the latest figure for one container."""
    _region().set(_PREFIX + uuid, figure)


def recall(uuid):
    """The latest figure for one container, or None if none is current."""
    found = _region().get(_PREFIX + uuid)
    return None if found is cache.NO_VALUE else found


def recall_many(uuids):
    """Figures for several containers at once, absent where unknown."""
    if not uuids:
        return {}
    keys = [_PREFIX + u for u in uuids]
    found = {}
    for uuid, value in zip(uuids, _region().get_multi(keys)):
        if value is not cache.NO_VALUE:
            found[uuid] = value
    return found
