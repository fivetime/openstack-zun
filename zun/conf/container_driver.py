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

driver_opts = [
    cfg.StrOpt('container_driver',
               default='docker',
               help="""Defines which driver to use for controlling container.
Possible values:

* ``docker``
* ``cri`` (containers and capsules over the CRI runtime, no docker daemon
  required. `docker cp` -- get_archive/put_archive -- is the one thing it
  does not serve yet, so a fleet mixing this with ``docker`` answers that
  one command differently depending on where a container landed.)

Services which consume this:

* ``zun-compute``

Interdependencies to other options:

* None
"""),
    cfg.StrOpt('capsule_driver',
               default='cri',
               help="""Defines which driver to use for controlling capsule.
Possible values:

* ``docker``
* ``cri``

Services which consume this:

* ``zun-compute``

Interdependencies to other options:

* None
"""),
    cfg.IntOpt('default_sleep_time', default=1,
               help='Time to sleep (in seconds) during waiting for an event.'),
    cfg.IntOpt('default_timeout', default=60 * 10,
               help='Maximum time (in seconds) to wait for an event.'),
    cfg.StrOpt('floating_cpu_set',
               default="",
               help='Define the cpusets to be excluded from pinning'),
    cfg.StrOpt('container_runtime', default='runc',
               help='Define the runtime to create container with. '
                    'Default value in Zun is ``runc``.'),
    cfg.IntOpt('default_memory_swap',
               default=-1,
               help='The default memory swap size in MB (default is -1 '
                    'which enable unlimited swap).'),
    cfg.IntOpt('minimum_memory',
               default=4,
               help='The minimum memory size in MB allowed to set '
                    'when run/create container.'),
    cfg.IntOpt('maximum_memory',
               default=8192,
               help='The maximum memory size in MB allowed to set '
                    'when run/create container.'),
    cfg.FloatOpt('minimum_cpus',
                 default=0.1,
                 help='The minimum number of virtual cpus allowed to set '
                 'when run/create container.'),
    cfg.FloatOpt('maximum_cpus',
                 default=16.0,
                 help='The maximum number of virtual cpus allowed to set '
                 'when run/create container.'),
    cfg.IntOpt('minimum_disk',
               default=1,
               help='The minimum disk size in GB that user can set '
                    'when run/create container.'),
    cfg.IntOpt('maximum_disk',
               default=160,
               help='The maximum disk size in GB that user can set '
                    'when run/create container.'),
    cfg.StrOpt('probe_helper_path',
               default='/opt/kubezun/probe',
               help='Directory on this host holding the probe helper, mounted '
                    'read-only into any container that declares a probe. A '
                    'probe must run inside the container -- nothing outside '
                    'can reach it -- and a distroless image has no shell, curl '
                    'or wget to run, so without this a container that is '
                    'answering perfectly well reports as unhealthy. Empty '
                    'disables the mount.'),
    cfg.StrOpt('probe_helper_mount',
               default='/.kubezun',
               help='Where the probe helper appears inside the container. '
                    'Chosen to be somewhere no image is likely to use; the '
                    'rewritten probe command has to agree with it.'),
    cfg.ListOpt('allowed_capabilities',
                default=['NET_BIND_SERVICE'],
                help='Linux capabilities a capsule container may ADD through '
                     'its securityContext. Anything outside this list is '
                     'refused rather than silently dropped. The default is the '
                     'one capability Kubernetes PodSecurity "restricted" itself '
                     'allows -- binding a privileged port -- because a tenant '
                     'that can add arbitrary capabilities (SYS_ADMIN, '
                     'NET_ADMIN, ...) has, inside its Kata guest, most of what '
                     '"privileged" would have granted, which this platform '
                     'refuses outright. Dropping capabilities is never '
                     'restricted. Widen this only for a trusted tenant on a '
                     'dedicated compute host.'),
    cfg.IntOpt('default_memory',
               default=512,
               help='The default memory in MB a container can use '
                    '(will be used if user do not specify '
                    'container\'s memory). This value should be '
                    'in range [minimum_memory, maximum_memory].'),
    cfg.FloatOpt('default_cpu',
                 default=1.0,
                 help='The default number of cpus a container can use '
                 '(will be used if user do not specify '
                 'a container\'s cpus). This value should be '
                 'in range [minimum_cpus, maximum_cpus]'),
    cfg.IntOpt('cri_exec_timeout',
               default=30,
               help='Seconds a command run through the capsule exec endpoint '
                    'may take before the runtime kills it. Keep it below '
                    '[DEFAULT] rpc_response_timeout: a command that outlives '
                    'that never sends a reply, so the caller sees a server '
                    'error instead of the timeout that actually happened.'),
    cfg.StrOpt('cri_snapshotter',
               default='overlayfs',
               help='Name of the containerd snapshotter the CRI is '
                    'configured with. Restarting an exited container '
                    'carries its writable layer into the replacement by '
                    'asking this snapshotter where the layer lives; the '
                    'name must match [plugins.cri.containerd] snapshotter '
                    'on the node or the lookup finds nothing and a restart '
                    'falls back to a fresh container from the image.'),
    cfg.StrOpt('cri_log_root',
               default='/var/log/zun/capsules',
               help='Directory the runtime writes capsule container logs to. '
                    'Nothing prunes it, so on a busy compute node give it a '
                    'filesystem that can be rotated independently.'),
    cfg.StrOpt('cri_sandbox_cgroup_parent',
               default='zun.slice',
               help='Cgroup slice each capsule sandbox is placed under, one '
                    'child per capsule. Naming it is not optional bookkeeping: '
                    'the runtime reports a sandbox no cgroup as having no '
                    'accounting at all, and the stats call that carries the '
                    'network counters fails outright rather than returning '
                    'what it can. On a kata capsule the figures under here '
                    'describe the virtual machine, which is the right unit '
                    'for a node and the wrong one for a container.'),
    cfg.IntOpt('default_disk',
               default=10,
               help='The default disk size a container can use '
                    '(will be used if user do not specify '
                    'container\'s disk). This value should be '
                    'in range [minimum_disk, maximum_disk]. Default '
                    'is 10 (GiB).')
]


ALL_OPTS = (driver_opts)


def register_opts(conf):
    conf.register_opts(ALL_OPTS)


def list_opts():
    return {"DEFAULT": ALL_OPTS}
