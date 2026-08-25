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

from oslo_cache import core as cache
from oslo_config import cfg

usage_group = cfg.OptGroup(
    name='usage',
    title='How much a container is using, reported rather than asked',
    help="""A compute node measures what its containers take and says so
on the notification bus; the API keeps the latest figure it heard and
serves it. Nothing is written to the database: this is a measurement,
not state, and a figure that is lost is replaced by the next one.""")

usage_opts = [
    cfg.IntOpt('report_interval',
               default=60, min=10,
               help="""Seconds between one node's usage reports.

Measuring a container's writable layer walks its upper directory, so the
cost grows with the number of files there rather than the bytes. This is
why the figure is reported on a schedule instead of computed when asked:
a tenant asking twice costs the node nothing extra."""),
    cfg.IntOpt('retain_reports',
               default=3, min=1,
               help="""How many report intervals a figure stays served for.

After this many intervals with nothing heard from a node, the figure is
gone and the field reads as unknown -- which is right: a node that has
stopped reporting is a node whose containers may no longer be there."""),
    cfg.StrOpt('listener_pool',
               default='zun-api-usage',
               help="""The consumer group the API listens as.

Every API replica joins the same group, so the bus hands each report to
one of them and never to two. Change it only if a second consumer of the
same reports must see every one."""),
]


def register_opts(conf):
    conf.register_group(usage_group)
    conf.register_opts(usage_opts, group=usage_group)
    # The standard [cache] section every OpenStack service exposes, with
    # dogpile behind it. Left disabled, a process-local dictionary stands
    # in and each API replica remembers only what it heard itself -- fine
    # for one replica, and honest for more, since the field just reads as
    # unknown on the replica that did not hear. Point it at memcached and
    # the replicas share.
    cache.configure(conf)


def list_opts():
    return {usage_group: usage_opts}
