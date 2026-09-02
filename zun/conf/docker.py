# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo_config import cfg

docker_group = cfg.OptGroup(name='docker',
                            title='Options for docker')

docker_opts = [
    cfg.BoolOpt('verify_wiring',
                default=True,
                help='After starting a container, check that the host-side '
                     'interface kuryr makes for each of its neutron ports '
                     'actually exists, and refuse the start when it does '
                     'not. Every status said the container was fine while '
                     'its packets went nowhere: docker said running, the '
                     'port said ACTIVE -- the one witness that does not '
                     'lie is the interface in the host network namespace. '
                     'Assumes zun-compute shares the host network '
                     'namespace, as the charts deploy it; turn it off '
                     'where it does not.'),
    cfg.IntOpt('verify_wiring_timeout',
               default=10, min=1,
               help='Seconds to wait for the host-side interface to appear '
                    'after a start before calling the container unwired. '
                    'Plumbing is ordinarily complete when docker start '
                    'returns; this covers a slow node, not a broken one.'),
    cfg.StrOpt('docker_remote_api_version',
               default='1.26',
               help='Docker remote api version. Override it according to '
                    'specific docker api version in your environment.'),
    cfg.IntOpt('default_timeout',
               default=45,
               help='Seconds a call to the container runtime may take '
                    'before the client gives up. Keep it below [DEFAULT] '
                    'rpc_response_timeout: at sixty, the same as that one, '
                    'the two expire together and the RPC always loses -- so '
                    'every slow runtime call reached the caller as "the node '
                    'stopped answering" rather than as what actually went '
                    'wrong. Seen with `docker update --cpus` on a VM '
                    'runtime, where the runtime hangs and the real error was '
                    'never delivered.'),
    cfg.StrOpt('api_url',
               default='unix:///var/run/docker.sock',
               help='API endpoint of docker daemon'),
    cfg.StrOpt('docker_remote_api_url',
               default='tcp://$docker_remote_api_host:$docker_remote_api_port',
               help='Remote API endpoint of docker daemon'),
    cfg.BoolOpt('api_insecure',
                default=False,
                help='If set, ignore any SSL validation issues'),
    cfg.StrOpt('ca_file',
               help='Location of CA certificates file for '
                    'securing docker api requests (tlscacert).'),
    cfg.StrOpt('cert_file',
               help='Location of TLS certificate file for '
                    'securing docker api requests (tlscert).'),
    cfg.StrOpt('key_file',
               help='Location of TLS private key file for '
                    'securing docker api requests (tlskey).'),
    cfg.StrOpt('docker_remote_api_host',
               default='$my_ip',
               help='Defines the remote api host for the docker daemon.'),
    cfg.StrOpt('docker_remote_api_port',
               default='2375',
               help='Defines the remote api port for the docker daemon.'),
    cfg.IntOpt('execute_timeout',
               default=30,
               help='Seconds a command run through the exec endpoint may take '
                    'before it is killed. The same limit the CRI driver keeps '
                    'as [container_driver] cri_exec_timeout, because the two '
                    'answer the same request and a caller cannot tell which '
                    'driver is behind it. Keep it below [DEFAULT] '
                    'rpc_response_timeout: a command that outlives that never '
                    'sends a reply, so the caller sees a server error instead '
                    'of the timeout that actually happened. Five, the old '
                    'default, is short enough that ordinary commands hit it -- '
                    'anything that waits on the network usually does.'),
    cfg.StrOpt('docker_data_root',
               default='/var/lib/docker',
               deprecated_for_removal=True,
               help='Root directory of persistent Docker state.'),
    cfg.IntOpt('default_swap',
               default=0,
               help='Swap in MB granted to a container that does not ask '
                    'for any. Zero, because most workloads want none and a '
                    'runtime default of twice the memory limit is a great '
                    'deal of swap to hand out by accident. -1 allows swap '
                    'without limit.'),
    cfg.IntOpt('default_pids_limit',
               default=-1,
               help='PidsLimit applied to containers that do not carry a '
                    'pids_limit of their own. A fork bomb in a runc '
                    'container exhausts the host PID table and scheduler '
                    'with it, so a node serving untrusted tenants should '
                    'set this. -1 leaves the runtime default (unlimited).'),
    cfg.StrOpt('default_registry',
               help='The default registry from which docker images are '
                    'pulled. Its value can be the registry domain name '
                    '(e.g. docker.io) or None.'),
    cfg.StrOpt('default_registry_username',
               help='The username of the default registry.'),
    cfg.StrOpt('default_registry_password',
               help='The password of the default registry.'),
]

ALL_OPTS = (docker_opts)


def register_opts(conf):
    conf.register_group(docker_group)
    conf.register_opts(ALL_OPTS, docker_group)


def list_opts():
    return {docker_group: ALL_OPTS}
