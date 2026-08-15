# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_config import cfg


network_group = cfg.OptGroup(name='network',
                             title='Options for the container network')

network_opts = [
    cfg.StrOpt('driver',
               default='kuryr',
               help='Defines which driver to use for container network.'),
    cfg.StrOpt('driver_name',
               default='kuryr',
               help=('The network plugin driver name, you can find it by'
                     ' docker plugin list.')),
    cfg.IntOpt('stale_network_min_age',
               default=120,
               help='Seconds a docker network row must have existed before '
                    'it may be treated as stale when docker no longer has '
                    'the network. Below this age the row is assumed to '
                    'belong to a create still in flight.'),
]

ALL_OPTS = (network_opts)


def register_opts(conf):
    conf.register_group(network_group)
    conf.register_opts(ALL_OPTS, group=network_group)


def list_opts():
    return {network_group: ALL_OPTS}
