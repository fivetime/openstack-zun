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


manila_group = cfg.OptGroup(name='manila_client',
                            title='Options for reaching Manila')

# The same two options every other client here has. Without them the NFS
# volume driver could only ask the catalog for the public endpoint, and a
# compute node is exactly the kind of host that may not resolve or reach it:
# a deployment that points cinder_client and placement_client at internal
# endpoints has to be able to do the same for Manila.
manila_client_opts = [
    cfg.StrOpt('region_name',
               help='Region in Identity service catalog to use for '
                    'communication with the OpenStack service.'),
    cfg.StrOpt('endpoint_type',
               default='publicURL',
               help='Type of endpoint in Identity service catalog to use '
                    'for communication with the OpenStack service.'),
]


def register_opts(conf):
    conf.register_group(manila_group)
    conf.register_opts(manila_client_opts, group=manila_group)


def list_opts():
    return {manila_group: manila_client_opts}
