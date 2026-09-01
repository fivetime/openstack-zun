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

import datetime
import errno
import eventlet
import functools
import os
import shutil
import types

from docker import errors
from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_utils import fileutils
from oslo_serialization import jsonutils
from oslo_utils import timeutils
from oslo_utils import uuidutils
import psutil
import tenacity

from zun.common import consts
from zun.common import exception
from zun.common.i18n import _
from zun.common import utils
from zun.common.utils import check_container_id
from zun.compute import container_actions
import zun.conf
from zun.container.docker import host
from zun.container.docker import utils as docker_utils
from zun.container import driver
from zun.container import orphan
from zun.image import driver as img_driver
from zun.network import network as zun_network
from zun.network import neutron
from zun import objects

CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)
ATTACH_FLAG = "/attach/ws?logs=0&stream=1&stdin=1&stdout=1&stderr=1"
# The same stream with nothing going the other way. A container created
# without a terminal has no stdin to attach to and refuses the session
# above; it still writes output, and this is how that output is followed.
LOGS_FLAG = "/attach/ws?logs=0&stream=1&stdin=0&stdout=%d&stderr=%d"


def is_not_found(e):
    return '404' in str(e)


def is_not_connected(e):
    # Test the following exception:
    #
    #   500 Server Error: Internal Server Error ("container XXX is not
    #   connected to the network XXX")
    #
    # Note(hongbin): Docker should response a 4xx instead of 500. This looks
    # like a bug from docker side: https://github.com/moby/moby/issues/35888
    return ' is not connected to the network ' in str(e)


def is_conflict(e):
    conflict_infos = ['not running', 'not paused', 'paused']
    for info in conflict_infos:
        if info in str(e):
            return True
    return False


def handle_not_found(e, context, container, do_not_raise=False):
    if container.status == consts.DELETING:
        return

    if container.auto_remove:
        container.status = consts.DELETED
    else:
        container.status = consts.ERROR
        container.status_reason = str(e)
    container.save(context)
    if do_not_raise:
        return

    raise exception.Conflict(message=_(
        "Cannot act on container in '%s' state") % container.status)


def wrap_docker_error(function):
    @functools.wraps(function)
    def decorated_function(*args, **kwargs):
        context = args[1]
        container = args[2]
        try:
            return function(*args, **kwargs)
        except exception.DockerError as e:
            if is_not_found(e):
                handle_not_found(e, context, container)
            if is_conflict(e):
                raise exception.Conflict(_("%s") % str(e))
            raise

    return decorated_function


def _counters(res):
    """The fields one reading carries.

    A single sample: the previous-reading fields are left for whoever
    holds the reading before this one, which is the cache the reports go
    into. one_shot leaves them empty here, and filling them with what
    docker would have sampled a second ago would be a rate over an
    interval nobody asked about.
    """
    cpu = res.get('cpu_stats') or {}
    memory = res.get('memory_stats') or {}
    out = {
        'timestamp': res.get('read'),
        'cpu': {
            'total_ns': (cpu.get('cpu_usage') or {}).get('total_usage'),
            'system_ns': cpu.get('system_cpu_usage'),
            'online_cpus': cpu.get('online_cpus'),
        },
        'memory': {
            'usage': memory.get('usage'),
            'limit': memory.get('limit'),
            'cache': _cache_usage(memory),
        },
        'pids': {'current': (res.get('pids_stats') or {}).get('current')},
    }
    networks = res.get('networks') or {}
    if networks:
        out['networks'] = {
            name: {'rx_bytes': v.get('rx_bytes'),
                   'tx_bytes': v.get('tx_bytes'),
                   'rx_packets': v.get('rx_packets'),
                   'tx_packets': v.get('tx_packets')}
            for name, v in networks.items()}
    # Left out rather than zeroed when the runtime only offered the
    # placeholder entries; see stats() for how those are told apart.
    measured = [i for i in
                ((res.get('blkio_stats') or {})
                 .get('io_service_bytes_recursive') or [])
                if i.get('major') or i.get('minor')]
    if measured:
        out['blkio'] = {
            'read_bytes': sum(i['value'] for i in measured
                              if i['op'].lower() == 'read'),
            'write_bytes': sum(i['value'] for i in measured
                               if i['op'].lower() == 'write'),
        }
    return out


def _cache_usage(memory_stats):
    """Page cache inside a container's memory figure.

    Counted as used by the kernel and reclaimable in practice, so what a
    reader means by "memory used" excludes it. The key differs between
    cgroup versions, which is the only reason this is not one lookup.
    """
    stats = (memory_stats or {}).get('stats') or {}
    return (stats.get('total_inactive_file')      # cgroup v1
            or stats.get('inactive_file')         # cgroup v2
            or 0)


def _network_lock(neutron_net_id):
    """One lock per (host, neutron network); provision and release share it."""
    return '%snetwork-%s' % (consts.NAME_PREFIX, neutron_net_id)


class DockerDriver(driver.BaseDriver, driver.ContainerDriver,
                   driver.CapsuleDriver):
    """Implementation of container drivers for Docker."""

    # TODO(hongbin): define a list of capabilities of this driver.
    capabilities = {}

    def __init__(self):
        super(DockerDriver, self).__init__()
        self._host = host.Host()
        self._get_host_storage_info()
        self.image_drivers = {}
        for driver_name in CONF.image_driver_list:
            driver = img_driver.load_image_driver(driver_name)
            self.image_drivers[driver_name] = driver

    def _get_host_storage_info(self):
        host_info = self.get_host_info()
        self.docker_root_dir = host_info['docker_root_dir']
        storage_info = self._host.get_storage_info()
        self.base_device_size = storage_info['default_base_size']
        self.support_disk_quota = self._host.check_supported_disk_quota(
            host_info)

    def load_image(self, image_path=None):
        with docker_utils.docker_client() as docker:
            if image_path:
                with open(image_path, 'rb') as fd:
                    LOG.debug('Loading local image %s into docker', image_path)
                    docker.load_image(fd)

    def inspect_image(self, image):
        with docker_utils.docker_client() as docker:
            LOG.debug('Inspecting image %s', image)
            return docker.inspect_image(image)

    def get_image(self, name):
        LOG.debug('Obtaining image %s', name)
        with docker_utils.docker_client() as docker:
            return docker.get_image(name)

    def delete_image(self, context, img_id, image_driver=None):
        image = self.inspect_image(img_id)['RepoTags'][0]
        if image_driver:
            image_driver_list = [image_driver.lower()]
        else:
            image_driver_list = CONF.image_driver_list
        for driver_name in image_driver_list:
            try:
                image_driver = img_driver.load_image_driver(driver_name)
                if driver_name == 'glance':
                    image_driver.delete_image_tar(context, image)
                elif driver_name == 'docker':
                    image_driver.delete_image(context, img_id)
            except exception.ZunException:
                LOG.exception('Unknown exception occurred while deleting '
                              'image %s', img_id)

    def delete_committed_image(self, context, img_id, image_driver):
        try:
            image_driver.delete_committed_image(context, img_id)
        except Exception as e:
            LOG.exception('Unknown exception occurred while '
                          'deleting image %s: %s',
                          img_id,
                          str(e))
            raise exception.ZunException(str(e))

    def images(self, repo, quiet=False):
        with docker_utils.docker_client() as docker:
            return docker.images(repo, quiet)

    def pull_image(self, context, repo, tag, image_pull_policy='always',
                   driver_name=None, registry=None):
        if driver_name is None:
            driver_name = CONF.default_image_driver

        try:
            image_driver = self.image_drivers[driver_name]
            image, image_loaded = image_driver.pull_image(
                context, repo, tag, image_pull_policy, registry)
            if image:
                image['driver'] = driver_name.split('.')[0]
        except exception.ZunException:
            raise
        except Exception as e:
            LOG.exception('Unknown exception occurred while loading '
                          'image: %s', str(e))
            raise exception.ZunException(str(e))

        return image, image_loaded

    def search_image(self, context, repo, tag, driver_name, exact_match):
        if driver_name is None:
            driver_name = CONF.default_image_driver

        try:
            image_driver = self.image_drivers[driver_name]
            return image_driver.search_image(context, repo, tag,
                                             exact_match)
        except exception.ZunException:
            raise
        except Exception as e:
            LOG.exception('Unknown exception occurred while searching '
                          'for image: %s', str(e))
            raise exception.ZunException(str(e))

    def push_image(self, context, repo, tag, registry, image_driver):
        """Send a committed image to the registry it is named for."""
        try:
            image_driver.push_image(context, repo, tag, registry)
        except Exception as e:
            LOG.exception('Unknown exception occurred while pushing '
                          'image: %s', str(e))
            raise exception.ZunException(str(e))

    def create_image(self, context, image_name, image_driver):
        try:
            img = image_driver.create_image(context, image_name)
        except Exception as e:
            LOG.exception('Unknown exception occurred while creating '
                          'image: %s', str(e))
            raise exception.ZunException(str(e))
        return img

    def upload_image_data(self, context, image, image_tag, image_data,
                          image_driver):
        try:
            image_driver.update_image(context,
                                      image.id,
                                      tag=image_tag)
            # Image data has to match the image format.
            # contain format defaults to 'docker';
            # disk format defaults to 'qcow2'.
            img = image_driver.upload_image_data(context,
                                                 image.id,
                                                 image_data)
        except Exception as e:
            LOG.exception('Unknown exception occurred while uploading '
                          'image: %s', str(e))
            raise exception.ZunException(str(e))
        return img

    def read_tar_image(self, image):
        with docker_utils.docker_client() as docker:
            LOG.debug('Reading local tar image %s ', image['path'])
            try:
                docker.read_tar_image(image)
            except Exception:
                LOG.warning("Unable to read image data from tarfile")

    def create(self, context, container, image, requested_networks,
               requested_volumes):
        with docker_utils.docker_client() as docker:
            network_driver = zun_network.driver(context=context,
                                                docker_api=docker)
            name = container.name
            if image['tag']:
                image_repo = image['repo'] + ":" + image['tag']
            else:
                image_repo = image['repo']
            LOG.debug('Creating container with image %(image)s name %(name)s',
                      {'image': image_repo, 'name': name})
            self._provision_network(context, network_driver,
                                    requested_networks)
            volmaps = requested_volumes.get(container.uuid, [])
            binds = self._get_binds(context, volmaps)

            entrypoint = container.entrypoint
            command = container.command
            if not entrypoint or not command:
                image_dict = docker.inspect_image(image_repo)
                container.entrypoint = entrypoint or \
                    image_dict['Config']['Entrypoint']
                container.command = command or image_dict['Config']['Cmd']
            kwargs = {
                'name': self.get_container_name(container),
                'command': container.command,
                'environment': container.environment,
                'working_dir': container.workdir,
                'labels': container.labels,
                'tty': container.tty,
                'stdin_open': container.interactive,
                'hostname': container.hostname,
                'entrypoint': container.entrypoint,
            }
            # Which user the process runs as. Left out when nothing asked,
            # so the image's own USER still decides -- passing '' would
            # override it with root, which is the opposite of what an
            # unset field means.
            if container.user:
                kwargs['user'] = container.user

            if not self._is_runtime_supported():
                if container.runtime:
                    raise exception.ZunException(_(
                        'Specifying runtime in Docker API is not supported'))
                runtime = None
            else:
                runtime = container.runtime or CONF.container_runtime

            host_config = {}
            host_config['privileged'] = container.privileged
            self._apply_security_context(container, kwargs, host_config)
            host_config['runtime'] = runtime
            host_config['binds'] = binds
            kwargs['volumes'] = [b['bind'] for b in binds.values()]
            self._declare_exposed_ports(container, kwargs)
            # Process the first requested network at create time. The rest
            # will be processed after create.
            requested_network = requested_networks.pop()
            security_group_ids = utils.get_security_group_ids(
                context, container.security_groups)
            network_driver.process_networking_config(
                container, requested_network, host_config, kwargs, docker,
                security_group_ids=security_group_ids)
            if container.auto_remove:
                host_config['auto_remove'] = container.auto_remove
            if self._should_limit_memory(container):
                host_config['mem_limit'] = str(container.memory) + 'M'
            if self._should_limit_cpu(container):
                host_config['cpu_shares'] = int(1024 * container.cpu)
            if container.restart_policy:
                count = int(container.restart_policy['MaximumRetryCount'])
                name = container.restart_policy['Name']
                host_config['restart_policy'] = {'Name': name,
                                                 'MaximumRetryCount': count}

            if container.disk:
                disk_size = str(container.disk) + 'G'
                host_config['storage_opt'] = {'size': disk_size}
            self._apply_flavor_limits(container, host_config)
            if container.cpu_policy == 'dedicated':
                host_config['cpuset_cpus'] = container.cpuset.cpuset_cpus
                host_config['cpuset_mems'] = str(container.cpuset.cpuset_mems)
            # The time unit in docker of heath checking is us, and the unit
            # of interval and timeout is seconds.
            # Handed to docker so it writes them into the container's
            # resolv.conf. Both are needed together: a search domain with
            # an inherited nameserver still has the query forwarded from
            # the host's namespace, where the tenant's DNS is not visible.
            if container.dns:
                host_config['dns'] = container.dns
            if container.dns_search:
                host_config['dns_search'] = container.dns_search
            resolv_conf = self._write_resolv_conf(container)
            if resolv_conf:
                binds[resolv_conf] = {'bind': '/etc/resolv.conf',
                                      'ro': True}
                host_config['binds'] = binds
                kwargs['volumes'] = [b['bind'] for b in binds.values()]
            if container.healthcheck:
                healthcheck = {}
                healthcheck['test'] = container.healthcheck.get('test', '')
                interval = container.healthcheck.get('interval', 0)
                healthcheck['interval'] = interval * 10 ** 9
                healthcheck['retries'] = int(container.healthcheck.
                                             get('retries', 0))
                timeout = container.healthcheck.get('timeout', 0)
                healthcheck['timeout'] = timeout * 10 ** 9
                kwargs['healthcheck'] = healthcheck

            kwargs['host_config'] = docker.create_host_config(**host_config)
            response = docker.create_container(image_repo, **kwargs)
            container.container_id = response['Id']

            addresses = self._setup_network_for_container(
                context, container, requested_networks, network_driver)
            container.addresses = addresses

            response = docker.inspect_container(container.container_id)
            self._populate_container(container, response, force=True)
            container.save(context)
            return container

    def _should_limit_memory(self, container):
        return (container.memory is not None and
                not isinstance(container, objects.Capsule))

    def _should_limit_cpu(self, container):
        return (container.cpu is not None and
                not isinstance(container, objects.Capsule))

    def _is_runtime_supported(self):
        return float(CONF.docker.docker_remote_api_version) >= 1.26

    def node_support_disk_quota(self):
        return self.support_disk_quota

    def _apply_security_context(self, container, kwargs, host_config):
        """Translate a capsule's securityContext onto docker's create args.

        The CRI driver has applied this since securityContext arrived; this
        driver never read it, so the same pod spec landed on a docker host
        as root, with a writable root filesystem and every capability the
        image had. What is dropped here is a *tightening*, which is the
        dangerous half of a silent drop.

        Only what was asked for is set, as on the CRI side: a field left
        out means whatever docker does by default, which is what an unset
        securityContext field means in Kubernetes too. The mapping mirrors
        _linux_security_context() field for field:

          runAsUser/runAsGroup        -> user "uid[:gid]" (overrides the
                                         container's own `user`, the less
                                         specific of the two requests)
          fsGroup                     -> group_add, so the process is IN the
                                         group the volume was chowned to
          readOnlyRootFilesystem      -> read_only
          allowPrivilegeEscalation:F  -> security_opt no-new-privileges
          capabilities.add/drop       -> cap_add (filtered by
                                         allowed_capabilities) / cap_drop
          seccompProfile Unconfined   -> security_opt seccomp=unconfined;
                                         RuntimeDefault is docker's default
                                         and needs nothing
        """
        sc = driver.security_context_of(container)
        if not sc:
            return
        security_opt = list(host_config.get('security_opt') or [])

        if sc.get('runAsUser') is not None:
            user = str(int(sc['runAsUser']))
            if sc.get('runAsGroup') is not None:
                user += ':%d' % int(sc['runAsGroup'])
            kwargs['user'] = user
        elif sc.get('runAsGroup') is not None:
            # A group without a user: docker takes "uid:gid" only, and the
            # uid is the image's to decide. Adding the group as supplemental
            # is what a kubelet ends up doing for the process too.
            host_config.setdefault('group_add', []).append(
                str(int(sc['runAsGroup'])))
        if sc.get('fsGroup') is not None:
            host_config.setdefault('group_add', []).append(
                str(int(sc['fsGroup'])))
        if sc.get('readOnlyRootFilesystem'):
            host_config['read_only'] = True
        if sc.get('allowPrivilegeEscalation') is False:
            security_opt.append('no-new-privileges')

        caps = sc.get('capabilities') or {}
        if caps.get('add') or caps.get('drop'):
            # Second line of defence behind the API's validation, the same
            # one the CRI driver keeps: a forbidden capability that reached
            # the stored spec does not reach the runtime. Dropping is never
            # restricted.
            allowed = {c.upper() for c in CONF.allowed_capabilities}
            asked = [str(c).upper() for c in (caps.get('add') or [])]
            add = [c for c in asked if c in allowed]
            refused = [c for c in asked if c not in allowed]
            if refused:
                LOG.warning("refusing capabilities %s on container %s; this "
                            "host allows adding only %s",
                            refused, container.uuid,
                            sorted(allowed) or '(none)')
            if add:
                host_config['cap_add'] = add
            if caps.get('drop'):
                host_config['cap_drop'] = [str(c).upper()
                                           for c in caps['drop']]

        profile = sc.get('seccompProfile') or {}
        kind = profile.get('type')
        if kind == 'Unconfined':
            security_opt.append('seccomp=unconfined')
        elif kind == 'Localhost':
            # Refused at the API; should never arrive. Docker's default is
            # the stricter of the two, and a tenant-named host path is
            # exactly what must not be handed to the runtime.
            LOG.warning("ignoring unsupported Localhost seccomp profile on "
                        "container %s; using docker's default", container.uuid)

        if security_opt:
            host_config['security_opt'] = security_opt

    def _apply_flavor_limits(self, container, host_config):
        """Translate the flavor limit fields into docker HostConfig.

        Every field is optional; an absent one falls back to the operator
        default where one exists (pids, swap) or to the runtime default.
        The io device caps target the disk backing the docker data root,
        resolved once per driver -- a container cannot name a device, so it
        cannot throttle (or unthrottle) anything but its own rootfs path.
        """
        pids = container.pids_limit
        if pids is None and CONF.docker.default_pids_limit > 0:
            pids = CONF.docker.default_pids_limit
        if pids is not None:
            host_config['pids_limit'] = pids

        host_config['memswap_limit'] = self._memswap_limit(container)

        if container.blkio_weight is not None:
            host_config['blkio_weight'] = container.blkio_weight

        dev = None
        caps = [('device_read_bps', container.device_read_bps),
                ('device_write_bps', container.device_write_bps),
                ('device_read_iops', container.device_read_iops),
                ('device_write_iops', container.device_write_iops)]
        if any(v is not None for _, v in caps):
            dev = self._get_rootfs_device()
        if dev:
            for key, value in caps:
                if value is not None:
                    host_config[key] = [{'Path': dev, 'Rate': value}]

    def reap_orphans(self, context, min_age, dry_run=False):
        """Reap containerd tasks in the moby namespace dockerd disowns.

        dockerd delegates to containerd but keeps its own record of what it
        asked for, and when that record and the runtime disagree nothing
        reconciles them: a create that failed after the sandbox came up, a
        force-delete that could not clear a stale task (kata does this), a
        data root replaced under a running daemon. The task keeps running --
        with kata, a whole VM -- and `docker ps` shows nothing.

        The authority here is dockerd: an id it cannot inspect belongs to no
        container of ours. Only the moby namespace is looked at; k8s.io is
        the kubelet's and none of our business.
        """
        objects_ = []
        try:
            out, _err = utils.execute(
                'ctr', '--namespace', 'moby', 'containers', 'list', '-q',
                run_as_root=True)
        except Exception as e:
            LOG.debug('Orphan sweep skipped, cannot list containerd: %s', e)
            return (0, 0, 0)

        for cid in [line.strip() for line in out.splitlines() if line.strip()]:
            objects_.append(orphan.RuntimeObject(
                cid, self._containerd_age(cid), label=cid[:12]))

        def is_claimed(obj):
            with docker_utils.docker_client() as docker:
                try:
                    docker.inspect_container(obj.ident)
                    return True
                except errors.APIError as e:
                    if is_not_found(e):
                        return False
                    raise

        def remove(obj):
            for args in (('tasks', 'kill', '-s', 'SIGKILL', obj.ident),
                         ('tasks', 'delete', '--force', obj.ident),
                         ('containers', 'delete', obj.ident)):
                try:
                    utils.execute('ctr', '--namespace', 'moby', *args,
                                  run_as_root=True)
                except Exception as e:
                    # A task that is already gone makes the first two fail;
                    # only the last one failing means the record survives.
                    LOG.debug('ctr %(args)s on %(id)s: %(err)s',
                              {'args': args[:2], 'id': obj.label, 'err': e})

        return orphan.sweep('moby', objects_, is_claimed, remove, min_age,
                            dry_run=dry_run)

    @staticmethod
    def _memswap_limit(container, memory=None):
        """What the runtime is told, from the swap the caller asked for.

        The runtime takes memory and swap as one total; nothing above this
        layer should have to know that. A swap of 0 -- the default, because
        most workloads want none -- becomes a total equal to the memory,
        which is how the runtime is told "no swap". -1 stays -1: unlimited
        is a sentinel, not a quantity to add to anything.
        """
        swap = container.swap
        if swap is None:
            swap = CONF.docker.default_swap
        if swap == -1:
            return -1
        memory = memory if memory is not None else container.memory
        try:
            return str(int(memory) + int(swap)) + 'M'
        except (TypeError, ValueError):
            # No memory to add it to; let the runtime keep its own default
            # rather than send it something invented.
            return None

    @staticmethod
    def _containerd_age(cid):
        """Seconds since containerd created this container record."""
        try:
            out, _err = utils.execute(
                'ctr', '--namespace', 'moby', 'containers', 'info', cid,
                run_as_root=True)
            created = jsonutils.loads(out).get('CreatedAt')
            if not created:
                return None
            stamp = timeutils.parse_isotime(created)
            return (timeutils.utcnow(with_timezone=True) -
                    stamp).total_seconds()
        except Exception as e:
            LOG.debug('Cannot read age of containerd container %(id)s: '
                      '%(err)s', {'id': cid, 'err': e})
            return None

    def _apply_volume_io_limits(self, context, container):
        """Cap io on the devices this container's volumes resolve to.

        Ceph will not do this for us: rbd QoS lives in librbd and the local
        attach is krbd, which never enters it. The node is the only place
        left, and the cgroup of the container is the only handle there.

        Written after start, and again on every start: the scope is created
        by the runtime when the container starts and taken away when it
        stops, so an io.max written earlier is not merely stale, it has
        nowhere to live. Failure is logged, never fatal -- a container that
        runs uncapped is a billing question, while one that refuses to start
        because a cgroup file moved is an outage.
        """
        if not container.container_id:
            return
        try:
            volmaps = objects.VolumeMapping.list_by_container(
                context, container.uuid)
        except Exception as e:
            LOG.warning('Cannot list volumes of container %(c)s to apply io '
                        'limits: %(e)s', {'c': container.uuid, 'e': e})
            return

        rules = []
        for volmap in volmaps:
            caps = {'rbps': volmap.read_bps, 'wbps': volmap.write_bps,
                    'riops': volmap.read_iops, 'wiops': volmap.write_iops}
            if not any(v is not None for v in caps.values()):
                continue
            devno = self._volume_device_number(volmap)
            if not devno:
                continue
            terms = ' '.join('%s=%s' % (k, v)
                             for k, v in caps.items() if v is not None)
            rules.append('%s %s' % (devno, terms))
        if not rules:
            return

        scope = ('/sys/fs/cgroup/system.slice/docker-%s.scope/io.max'
                 % container.container_id)
        for rule in rules:
            try:
                utils.execute('sh', '-c',
                              'echo %s > %s' % (rule, scope),
                              run_as_root=True)
                LOG.info('Applied io limits "%(r)s" to container %(c)s',
                         {'r': rule, 'c': container.uuid})
            except Exception as e:
                LOG.warning('Failed to apply io limits "%(r)s" to container '
                            '%(c)s: %(e)s',
                            {'r': rule, 'c': container.uuid, 'e': e})

    @staticmethod
    def _volume_device_number(volmap):
        """MAJ:MIN of the block device behind an attached volume."""
        conn_info = volmap.connection_info
        if not conn_info:
            return None
        try:
            path = jsonutils.loads(conn_info)['data'].get('device_path')
        except Exception:
            return None
        if not path:
            return None
        try:
            st = os.stat(path)
            return '%d:%d' % (os.major(st.st_rdev), os.minor(st.st_rdev))
        except OSError as e:
            LOG.warning('Cannot stat volume device %(p)s: %(e)s',
                        {'p': path, 'e': e})
            return None

    _rootfs_device = None

    def _get_rootfs_device(self):
        """The whole disk behind the docker data root, e.g. /dev/vda.

        Throttle rules must name the disk, not the partition -- the io
        controller matches bios at the disk level and a partition dev_t
        silently matches nothing.
        """
        if self._rootfs_device:
            return self._rootfs_device
        try:
            src, _ = utils.execute(
                'findmnt', '-no', 'SOURCE', '--target',
                CONF.docker.docker_data_root)
            src = src.strip().split('[')[0]
            pk, _ = utils.execute('lsblk', '-no', 'pkname', src)
            pk = pk.strip().splitlines()[0].strip() if pk.strip() else ''
            self._rootfs_device = '/dev/' + pk if pk else src
        except Exception as e:
            LOG.warning('Cannot resolve the docker data root device, '
                        'io device caps will be skipped: %s', e)
            return None
        return self._rootfs_device

    def get_host_default_base_size(self):
        return self.base_device_size

    def _declare_exposed_ports(self, container, kwargs):
        """Tell docker which ports the container says it listens on.

        A declaration and nothing more, as docker's own ``--expose`` is: it
        is recorded, shown by inspect, and opens nothing. What can reach the
        container is decided by its security groups alone. This driver used
        to turn the declaration into a security group of its own with the
        ports open to 0.0.0.0/0 -- one group per container, on the axis
        that costs a cloud-wide recompute, against a default quota of ten --
        which nothing a docker user writes asks for.
        """
        exposed_ports = {}
        if isinstance(container, objects.Container):
            exposed_ports.update(container.exposed_ports or {})
        if isinstance(container, objects.Capsule):
            for member in (list(container.init_containers) +
                           list(container.containers)):
                exposed_ports.update(member.exposed_ports or {})
        if not exposed_ports:
            return
        ports = []
        for port in exposed_ports:
            port, proto = port.split('/')
            ports.append((port, proto))
        kwargs['ports'] = ports

    def _provision_network(self, context, network_driver, requested_networks):
        for rq_network in requested_networks:
            # Same lock as the release on delete: without it a container
            # being created here can find the network present, and then
            # find it gone, because the last container on it was removed
            # in between.
            with lockutils.lock(_network_lock(rq_network['network'])):
                network_driver.get_or_create_network(context,
                                                     rq_network['network'])

    def _write_resolv_conf(self, container):
        """A resolver the container can actually reach, or None.

        docker puts its own resolver at 127.0.0.11 in the resolv.conf of
        any container on a user-defined network, and keeps whatever was
        asked for as that resolver's upstream. That works when the
        container shares the host's network namespace, because the
        resolver listens there. Under a VM runtime it does not: the
        loopback address inside the guest has nothing behind it, and
        every lookup fails with connection refused -- so a compose file's
        `db` resolves nowhere while the network itself is fine.

        Bind-mounting the file is docker's own answer to this: a mount at
        /etc/resolv.conf makes it leave the file alone. The address
        written here does not have to be one that answers, because OVN
        intercepts the query whatever it is addressed to; what matters is
        that it is reachable from inside the guest, which 127.0.0.11 is
        not.

        Only when the container asked for a resolver. Without that there
        is nothing better than docker's own arrangement to offer.
        """
        if not container.dns:
            return None
        directory = os.path.join(CONF.state_path, 'resolv',
                                 container.uuid)
        fileutils.ensure_tree(directory)
        path = os.path.join(directory, 'resolv.conf')
        lines = ['nameserver %s' % server for server in container.dns]
        if container.dns_search:
            lines.append('search %s' % ' '.join(container.dns_search))
        # A container's own name is one label, and the default of one dot
        # would send it to the search domain only after trying it whole.
        lines.append('options ndots:0')
        with open(path, 'w') as handle:
            handle.write('\n'.join(lines) + '\n')
        os.chmod(path, 0o644)
        return path

    def _remove_resolv_conf(self, container):
        """Take the file away with the container that used it."""
        directory = os.path.join(CONF.state_path, 'resolv', container.uuid)
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except Exception as exc:                            # noqa: BLE001
            LOG.warning('could not remove %s: %s', directory, exc)

    def _get_binds(self, context, requested_volumes):
        binds = {}
        for volume in requested_volumes:
            volume_driver = self._get_volume_driver(volume)
            source, destination = volume_driver.bind_mount(context, volume)
            binds[source] = {'bind': destination}
        return binds

    def _setup_network_for_container(self, context, container,
                                     requested_networks, network_driver):
        security_group_ids = utils.get_security_group_ids(
            context, container.security_groups)
        addresses = {}
        if container.addresses:
            addresses = container.addresses
        for network in requested_networks:
            if network['network'] in addresses:
                # This network is already setup so skip it
                continue

            addrs = network_driver.connect_container_to_network(
                container, network, security_groups=security_group_ids)
            addresses[network['network']] = addrs

        return addresses

    def delete(self, context, container, force):
        neutron_nets = list((container.addresses or {}).keys())
        with docker_utils.docker_client() as docker:
            try:
                network_driver = zun_network.driver(context=context,
                                                    docker_api=docker)
                self._cleanup_network_for_container(container, network_driver)
                if container.container_id:
                    docker.remove_container(container.container_id,
                                            force=force)
                self._remove_resolv_conf(container)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    self._remove_resolv_conf(container)
                    self._release_networks_left_unused(context, docker,
                                                       neutron_nets)
                    return
                if is_not_connected(api_error):
                    return
                raise
            self._release_networks_left_unused(context, docker, neutron_nets)

    def _release_networks_left_unused(self, context, docker, neutron_net_ids):
        """Drop this node's docker network for each one nothing here uses.

        The docker network is this node's wrapper for a neutron network,
        made on demand the first time a container here needs it
        (_provision_network). Its lifetime is that of the containers using
        it: when the last one goes, so does it -- the same way it came.

        That is what makes libnetwork release the subnetpool the IPAM
        driver made for it, and nothing else does. A network never
        removed is a pool never released, and enough of those with one
        name and the driver refuses to make the next, at which point no
        network can be created at all. Measured on a three-node
        deployment: 26 orphan networks and 54 unreleased pools, all
        because removal was left to an admin call nothing ever made.

        dockerd is asked which containers are still on the network,
        rather than the database: the two differ mid-delete, and
        dockerd's answer is the one that decides whether the remove
        would succeed. Under the same lock as provisioning, so a create
        on this node cannot slip in between the check and the remove.
        """
        for neutron_net_id in neutron_net_ids:
            with lockutils.lock(_network_lock(neutron_net_id)):
                try:
                    inspected = docker.inspect_network(neutron_net_id)
                except errors.APIError as api_error:
                    if is_not_found(api_error):
                        inspected = None
                    else:
                        raise
                if inspected is not None:
                    if inspected.get('Containers'):
                        continue
                    # A neutron network that is gone makes every host's
                    # wrapper of it garbage, whichever host looks first;
                    # nothing shares its pool any more but other garbage.
                    gone = self._neutron_network_is_gone(context,
                                                         neutron_net_id)
                    # Only when this is the last host wrapping it. Removing
                    # the docker network makes kuryr release the subnetpool
                    # -- and that pool is one neutron object shared by every
                    # host's docker network for this subnet. A node-scoped
                    # decision was deleting a cloud-scoped resource: the other
                    # hosts' networks kept the dead pool's id, and every
                    # container start on them failed with "No subnetpools
                    # with {'id': ...} is found" until the network was made
                    # again by hand. Measured in production on 2026-09-01,
                    # two of three hosts at once.
                    elsewhere = [
                        row.host for row in objects.ZunNetwork.list(
                            context,
                            filters={'neutron_net_id': neutron_net_id})
                        if row.host != CONF.host]
                    if elsewhere and not gone:
                        LOG.info('Kept docker network %s: nothing on this '
                                 'host uses it, but %s still wrap it and '
                                 'share its address pool',
                                 neutron_net_id, sorted(set(elsewhere)))
                        continue
                    try:
                        docker.remove_network(neutron_net_id)
                    except errors.APIError as api_error:
                        # The row stays with the network: dropping it would
                        # make the next sweep unable to find what it left.
                        LOG.warning('Could not remove docker network %s: %s',
                                    neutron_net_id, api_error)
                        continue
                    LOG.info('Removed docker network %s: no container on '
                             'this host uses it, and %s',
                             neutron_net_id,
                             'its neutron network is gone' if gone
                             else 'no other host wraps it')
                for row in objects.ZunNetwork.list(
                        context, filters={'neutron_net_id': neutron_net_id,
                                          'host': CONF.host}):
                    row.destroy()

    def _neutron_network_is_gone(self, context, neutron_net_id):
        """True only when neutron says the network does not exist.

        Not knowing is not the same as gone: a neutron that cannot be asked
        answers False, and the wrapper is kept for a sweep that can ask.
        """
        try:
            neutron.NeutronAPI(context).get_neutron_network(neutron_net_id)
        except exception.NetworkNotFound:
            return True
        except Exception as exc:
            LOG.warning('Cannot tell whether neutron network %s still '
                        'exists: %s', neutron_net_id, exc)
        return False

    def reclaim_stale_networks(self, context):
        """Sweep this node's docker networks for ones left behind.

        The wrapper for a neutron network is made when the first container
        here needs it and removed when the last one leaves -- on the delete
        path. A delete that failed before it got there, or a neutron
        network the tenant removed while the wrapper sat empty, leaves the
        wrapper standing with nothing to remove it: dockerd keeps it, and
        with it the address pool kuryr made. Measured on one node: two
        wrappers for compose networks whose neutron networks had been gone
        for two days, each holding a pool. The same decision the delete
        path makes, applied to everything this node recorded.
        """
        rows = objects.ZunNetwork.list(context, filters={'host': CONF.host})
        neutron_net_ids = sorted({row.neutron_net_id for row in rows
                                  if row.neutron_net_id})
        if not neutron_net_ids:
            return
        with docker_utils.docker_client() as docker:
            self._release_networks_left_unused(context, docker,
                                               neutron_net_ids)

    @wrap_docker_error
    def _cleanup_network_for_container(self, container, network_driver):
        if not container.addresses:
            return
        for neutron_net in container.addresses:
            network_driver.disconnect_container_from_network(
                container, neutron_net)

    def check_container_exist(self, container):
        with docker_utils.docker_client() as docker:
            docker_containers = [c['Id']
                                 for c in docker.list_containers()]
            if container.container_id not in docker_containers:
                return False
        return True

    def list(self, context):
        non_existent_containers = []
        with docker_utils.docker_client() as docker:
            docker_containers = docker.list_containers()
            id_to_container_map = {c['Id']: c
                                   for c in docker_containers}
            uuids = self._get_container_uuids(docker_containers)

        local_containers = self._get_local_containers(context, uuids)
        for container in local_containers:
            if container.status in (consts.CREATING, consts.DELETING,
                                    consts.DELETED):
                # Skip populating db record since the container is in a
                # unstable state.
                continue

            container_id = container.container_id
            docker_container = id_to_container_map.get(container_id)
            if not container_id or not docker_container:
                non_existent_containers.append(container)
                continue

            self._populate_container(container, docker_container)

        return local_containers, non_existent_containers

    def heal_with_rebuilding_container(self, context, container, manager):
        if not container.container_id:
            return

        rebuild_status = utils.VALID_STATES['rebuild']
        try:
            if (container.auto_heal and
                    container.status in rebuild_status):
                context.project_id = container.project_id
                objects.ContainerAction.action_start(
                    context, container.uuid, container_actions.REBUILD,
                    want_result=False)
                manager.container_rebuild(context, container)
            else:
                LOG.warning("Container %s was recorded in DB but "
                            "missing in docker", container.uuid)
                container.status = consts.ERROR
                msg = "No such container: %s in docker" % \
                      (container.container_id)
                container.status_reason = str(msg)
                container.save(context)
        except Exception as e:
            LOG.warning("heal container with rebuilding failed, "
                        "err code: %s", e)

    def _get_container_uuids(self, containers):
        # The name of Docker container is of the form '/zun-<uuid>'
        name_prefix = '/' + consts.NAME_PREFIX
        uuids = [c['Names'][0].replace(name_prefix, '', 1)
                 for c in containers]
        return [u for u in uuids if uuidutils.is_uuid_like(u)]

    def _get_local_containers(self, context, uuids):
        host_containers = objects.Container.list_by_host(context, CONF.host)
        uuids = list(set(uuids) | set([c.uuid for c in host_containers]))
        containers = objects.Container.list(context,
                                            filters={'uuid': uuids})
        return containers

    def sample_counters(self, context, containers):
        wanted = {c.container_id: c.uuid for c in containers
                  if c.container_id}
        if not wanted:
            return {}
        found = {}
        with docker_utils.docker_client() as docker:
            for container_id, uuid in wanted.items():
                try:
                    # one_shot: take the reading and return. Without it
                    # docker waits for a second sample so it can fill in
                    # precpu, which costs a second per container and is
                    # work we do not need -- the caller compares its own
                    # readings, taken a reporting interval apart.
                    res = docker.stats(container_id, decode=False,
                                       stream=False, one_shot=True)
                except Exception as exc:                    # noqa: BLE001
                    # One container the runtime would not answer about
                    # must not cost the host its whole report.
                    LOG.debug('could not sample %s: %s', uuid, exc)
                    continue
                found[uuid] = _counters(res)
        return found

    def list_local_images(self):
        with docker_utils.docker_client() as docker:
            listed = docker.images(all=False)
        out = []
        for entry in listed:
            image_id = entry.get('Id')
            if not image_id:
                continue
            out.append({
                'id': image_id,
                'tags': entry.get('RepoTags') or [],
                'size': int(entry.get('Size') or 0),
                # docker has no notion of a pinned image; the CRI does.
                'pinned': False,
            })
        return out

    def images_in_use(self):
        with docker_utils.docker_client() as docker:
            # all=True: a stopped container is one somebody may start, and
            # its image is what it would start from.
            listed = docker.containers(all=True)
        return {c.get('ImageID') for c in listed if c.get('ImageID')}

    def remove_local_image(self, image_id):
        with docker_utils.docker_client() as docker:
            docker.remove_image(image_id)

    def measure_writable_layers(self, context, containers):
        wanted = {c.container_id: c.uuid for c in containers
                  if c.container_id}
        if not wanted:
            return {}
        with docker_utils.docker_client() as docker:
            # size=True is what makes docker walk each container's upper
            # directory, and it is asked once for the host. SizeRw is the
            # writable layer alone; SizeRootFs would add the image and is
            # deliberately not read.
            listed = docker.containers(all=True, size=True)
        found = {}
        for entry in listed:
            uuid = wanted.get(entry.get('Id'))
            if uuid is None:
                continue
            size = entry.get('SizeRw')
            # docker leaves the key out, or null, for a container that has
            # written nothing. That is a real zero, not an unknown.
            found[uuid] = int(size) if size else 0
        return found

    def update_containers_states(self, context, containers, manager):
        local_containers, non_existent_containers = self.list(context)
        if not local_containers:
            return

        id_to_local_container_map = {container.container_id: container
                                     for container in local_containers
                                     if container.container_id}
        id_to_container_map = {container.container_id: container
                               for container in containers
                               if container.container_id}

        for cid in (id_to_container_map.keys() &
                    id_to_local_container_map.keys()):
            container = id_to_container_map[cid]
            # sync status
            local_container = id_to_local_container_map[cid]
            if container.status != local_container.status:
                old_status = container.status
                container.status = local_container.status
                container.save(context)
                LOG.info('Status of container %s changed from %s to %s',
                         container.uuid, old_status, container.status)
            # sync host
            # Note(kiennt): Current host.
            cur_host = CONF.host
            if container.host != cur_host:
                old_host = container.host
                container.host = cur_host
                container.save(context)
                LOG.info('Host of container %s changed from %s to %s',
                         container.uuid, old_host, container.host)
        for container in non_existent_containers:
            if container.host == CONF.host:
                if container.auto_remove:
                    container.status = consts.DELETED
                    container.save(context)
                else:
                    self.heal_with_rebuilding_container(context, container,
                                                        manager)

    def show(self, context, container):
        with docker_utils.docker_client() as docker:
            if container.container_id is None:
                return container

            response = None
            try:
                response = docker.inspect_container(container.container_id)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    handle_not_found(api_error, context, container,
                                     do_not_raise=True)
                    return container
                raise

            self._populate_container(container, response)
            return container

    def format_status_detail(self, status_time):
        try:
            st = datetime.datetime.strptime((status_time[:19]),
                                            '%Y-%m-%dT%H:%M:%S')
        except ValueError as e:
            LOG.exception("Error on parse {} : {}", (status_time, e))
            return

        if st == datetime.datetime(1, 1, 1):
            # return empty string if the time is January 1, year 1, 00:00:00
            return ""

        delta = timeutils.utcnow() - st
        time_dict = {}
        time_dict['days'] = delta.days
        time_dict['hours'] = delta.seconds // 3600
        time_dict['minutes'] = (delta.seconds % 3600) // 60
        time_dict['seconds'] = delta.seconds
        if time_dict['days']:
            return '{} days'.format(time_dict['days'])
        if time_dict['hours']:
            return '{} hours'.format(time_dict['hours'])
        if time_dict['minutes']:
            return '{} mins'.format(time_dict['minutes'])
        if time_dict['seconds']:
            return '{} seconds'.format(time_dict['seconds'])
        return

    def _populate_container(self, container, response, force=False):
        state = response.get('State')
        self._populate_container_state(container, state, force)

        config = response.get('Config')
        if config:
            self._populate_hostname_and_ports(container, config)

        hostconfig = response.get('HostConfig')
        if hostconfig:
            container.runtime = hostconfig.get('Runtime')

    def _populate_container_state(self, container, state, force):
        if container.task_state and not force:
            # NOTE(hongbin): we don't want to populate container state
            # if another thread is performing task on this container.
            # In this case, task_state will be assigned (which means there is a
            # task performing on this container) and 'force' will be set to
            # False. For example, if this method is called by create,
            # 'force' will be set to True to force refreshing the state.
            # If this method is called by list or show,
            # 'force' will be set to False, in which case we skip
            # refreshing the state if there is a task on this container.
            return

        if not state:
            LOG.warning('Receive unexpected state from docker: %s', state)
            container.status = consts.UNKNOWN
            container.status_reason = _("container state is missing")
            container.status_detail = None
        elif type(state) is dict:
            status_detail = ''
            # The runtime's verdict on the healthcheck the container was
            # created with. None when there is no healthcheck; otherwise
            # docker's own words -- starting, healthy, unhealthy -- which is
            # what a caller waiting for a dependency reads.
            container.health = (state.get('Health') or {}).get('Status')
            # Recorded whatever the container went on to become: the exit
            # code is what a caller scripts against, and reading it only in
            # the branch that formats a message would lose it for every
            # container that stopped without an error.
            if 'ExitCode' in state and not state.get('Running'):
                container.exit_code = state.get('ExitCode')
            if state.get('Error'):
                if state.get('Status') in ('exited', 'removing'):
                    container.status = consts.STOPPED
                else:
                    status = state.get('Status').capitalize()
                    if status in consts.CONTAINER_STATUSES:
                        container.status = status
                status_detail = self.format_status_detail(
                    state.get('FinishedAt'))
                container.status_detail = "Exited({}) {} ago (error)".format(
                    state.get('ExitCode'), status_detail)
            elif state.get('Paused'):
                container.status = consts.PAUSED
                status_detail = self.format_status_detail(
                    state.get('StartedAt'))
                container.status_detail = "Up {} (paused)".format(
                    status_detail)
            elif state.get('Restarting'):
                container.status = consts.RESTARTING
                container.status_detail = "Restarting"
            elif state.get('Running'):
                container.status = consts.RUNNING
                status_detail = self.format_status_detail(
                    state.get('StartedAt'))
                container.status_detail = "Up {}".format(
                    status_detail)
            elif state.get('Dead'):
                container.status = consts.DEAD
                container.status_detail = "Dead"
            else:
                started_at = self.format_status_detail(state.get('StartedAt'))
                finished_at = self.format_status_detail(
                    state.get('FinishedAt'))
                if started_at == "" and container.status == consts.CREATING:
                    container.status = consts.CREATED
                    container.status_detail = "Created"
                elif (started_at == "" and
                      container.status in (consts.CREATED, consts.RESTARTING,
                                           consts.ERROR, consts.REBUILDING)):
                    pass
                elif started_at != "" and finished_at == "":
                    LOG.warning('Receive unexpected state from docker: %s',
                                state)
                    container.status = consts.UNKNOWN
                    container.status_reason = _("unexpected container state")
                    container.status_detail = ""
                elif started_at != "" and finished_at != "":
                    container.status = consts.STOPPED
                    container.status_detail = "Exited({}) {} ago ".format(
                        state.get('ExitCode'), finished_at)
            if status_detail is None:
                container.status_detail = None
        else:
            state = state.lower()
            if state == 'created' and container.status == consts.CREATING:
                container.status = consts.CREATED
            elif (state == 'created' and
                  container.status in (consts.CREATED, consts.RESTARTING,
                                       consts.ERROR, consts.REBUILDING)):
                pass
            elif state == 'paused':
                container.status = consts.PAUSED
            elif state == 'running':
                container.status = consts.RUNNING
            elif state == 'dead':
                container.status = consts.DEAD
            elif state == 'restarting':
                container.status = consts.RESTARTING
            elif state in ('exited', 'removing'):
                container.status = consts.STOPPED
            else:
                LOG.warning('Receive unexpected state from docker: %s', state)
                container.status = consts.UNKNOWN
                container.status_reason = _("unexpected container state")
            container.status_detail = None

    def _populate_hostname_and_ports(self, container, config):
        # populate hostname only when container.hostname wasn't set
        if container.hostname is None:
            container.hostname = config.get('Hostname')
        # populate ports
        ports = []
        exposed_ports = config.get('ExposedPorts')
        if exposed_ports:
            for key in exposed_ports:
                port = key.split('/')[0]
                ports.append(int(port))
        container.ports = ports

    @check_container_id
    @wrap_docker_error
    def reboot(self, context, container, timeout):
        with docker_utils.docker_client() as docker:
            if timeout:
                docker.restart(container.container_id,
                               timeout=int(timeout))
            else:
                docker.restart(container.container_id)
            container.status = consts.RUNNING
            container.status_reason = None
            network_driver = zun_network.driver(context=context,
                                                docker_api=docker)
            network_driver.on_container_started(container)
            return container

    @check_container_id
    @wrap_docker_error
    def stop(self, context, container, timeout):
        with docker_utils.docker_client() as docker:
            if timeout:
                docker.stop(container.container_id,
                            timeout=int(timeout))
            else:
                docker.stop(container.container_id)
            container.status = consts.STOPPED
            container.status_reason = None
            network_driver = zun_network.driver(context=context,
                                                docker_api=docker)
            try:
                network_driver.on_container_stopped(container)
            except Exception as e:
                LOG.error('network driver failed on stopping container: %s',
                          str(e))
            return container

    @check_container_id
    @wrap_docker_error
    def start(self, context, container):
        with docker_utils.docker_client() as docker:
            docker.start(container.container_id)
            container.status = consts.RUNNING
            container.status_reason = None
            network_driver = zun_network.driver(context=context,
                                                docker_api=docker)
            try:
                network_driver.on_container_started(container)
            except Exception as e:
                LOG.error('network driver failed on starting container: %s',
                          str(e))
                try:
                    docker.stop(container.container_id, timeout=5)
                    LOG.debug('Stop container successfully')
                    network_driver.on_container_stopped(container)
                    LOG.debug('Network driver clean up successfully')
                except Exception:
                    pass
                container.status = consts.STOPPED
                container.status_reason = _("failed to configure network")
            self._apply_volume_io_limits(context, container)
            return container

    @check_container_id
    @wrap_docker_error
    def pause(self, context, container):
        """Freeze the container.

        ⚠️ Frozen, not released -- and not a difference between runtimes.
        This is the freezer cgroup, which stops processes being scheduled
        and gives nothing back: the memory stays allocated, and under a VM
        runtime the VMM still holds the whole of the guest's. The placement
        claim does not move either. CPU time stops being spent; that is all.

        docker means the same thing by the word, so this is not a divergence
        to fix. It is worth saying twice because a cloud charges for what is
        held, and a tenant reading "pause" may reasonably expect to stop
        paying. What they stop is the work, not the bill.
        """
        with docker_utils.docker_client() as docker:
            docker.pause(container.container_id)
            container.status = consts.PAUSED
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def unpause(self, context, container):
        with docker_utils.docker_client() as docker:
            docker.unpause(container.container_id)
            container.status = consts.RUNNING
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def show_logs(self, context, container, stdout=True, stderr=True,
                  timestamps=False, tail='all', since=None):
        with docker_utils.docker_client() as docker:
            try:
                tail = int(tail)
            except ValueError:
                tail = 'all'

            if since is None or since == 'None':
                return docker.logs(container.container_id, stdout, stderr,
                                   False, timestamps, tail, None)
            else:
                try:
                    since = int(since)
                except ValueError:
                    try:
                        since = datetime.datetime.strptime(
                            since, '%Y-%m-%d %H:%M:%S,%f')
                    except Exception:
                        raise
                return docker.logs(container.container_id, stdout, stderr,
                                   False, timestamps, tail, since)

    @check_container_id
    @wrap_docker_error
    def execute_create(self, context, container, command, interactive=False):
        stdin = True if interactive else False
        tty = True if interactive else False
        with docker_utils.docker_client() as docker:
            create_res = docker.exec_create(
                container.container_id, command, stdin=stdin, tty=tty)
            exec_id = create_res['Id']
            return exec_id

    def execute_run(self, exec_id, command):
        with docker_utils.docker_client() as docker:
            try:
                with eventlet.Timeout(CONF.docker.execute_timeout):
                    output = docker.exec_start(exec_id, False, False, False)
            except eventlet.Timeout:
                raise exception.Conflict(_(
                    "Timeout on executing command: %s") % command)
            inspect_res = docker.exec_inspect(exec_id)
            return output, inspect_res['ExitCode']

    def execute_resize(self, exec_id, height, width):
        height = int(height)
        width = int(width)
        with docker_utils.docker_client() as docker:
            try:
                docker.exec_resize(exec_id, height=height, width=width)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    raise exception.Invalid(_(
                        "no such exec instance: %s") % str(api_error))
                raise

    @check_container_id
    @wrap_docker_error
    def kill(self, context, container, signal=None):
        with docker_utils.docker_client() as docker:
            if signal is None or signal == 'None':
                docker.kill(container.container_id)
            else:
                docker.kill(container.container_id, signal)
            container.status = consts.STOPPED
            container.status_reason = None
            return container

    @check_container_id
    @wrap_docker_error
    def update(self, context, container):
        patch = container.obj_get_changes()

        args = {}
        memory = patch.get('memory')
        if memory is not None or 'swap' in patch:
            # The two travel together whichever of them moved: the runtime
            # is told a total, and a total sent without the memory it
            # contains is measured against whatever the runtime still holds.
            memory = memory if memory is not None else container.memory
            args['mem_limit'] = str(memory) + 'M'
            # Recomputed rather than carried: the swap the caller asked for
            # is a quantity of its own and does not change because the
            # memory limit did.
            args['memswap_limit'] = self._memswap_limit(container,
                                                        memory=memory)
        cpu = patch.get('cpu')
        if cpu is not None:
            args['cpu_shares'] = int(1024 * cpu)

        with docker_utils.docker_client() as docker:
            return docker.update_container(container.container_id, **args)

    @check_container_id
    def get_websocket_url(self, context, container):
        return self._stream_url(container, ATTACH_FLAG)

    @check_container_id
    def get_logs_url(self, context, container, stdout=True, stderr=True):
        return self._stream_url(container,
                                LOGS_FLAG % (int(bool(stdout)),
                                             int(bool(stderr))))

    def _stream_url(self, container, flag):
        """Where the daemon serves a stream for this container.

        Not the socket the rest of this driver talks over. A stream outlives
        the request that asked for it and is read by zun-wsproxy, which runs
        elsewhere, so it has to be an address that resolves off this node --
        which is why the daemon is told to listen on a port as well.
        """
        protocol = "wss" if (not CONF.docker.api_insecure and
                             CONF.docker.ca_file and
                             CONF.docker.key_file and
                             CONF.docker.cert_file) else "ws"
        version = CONF.docker.docker_remote_api_version
        remote_api_host = CONF.docker.docker_remote_api_host
        remote_api_port = CONF.docker.docker_remote_api_port
        return protocol + "://" + remote_api_host + ":" + remote_api_port \
            + "/v" + version + "/containers/" + container.container_id \
            + flag

    @check_container_id
    @wrap_docker_error
    def resize(self, context, container, height, width):
        with docker_utils.docker_client() as docker:
            height = int(height)
            width = int(width)
            docker.resize(container.container_id, height, width)
            return container

    @check_container_id
    @wrap_docker_error
    def top(self, context, container, ps_args=None):
        """The processes in the container, not the ones around it.

        docker answers this from the host, which is right when the
        container's processes are host processes. Under a VM runtime they
        are not: the only host process is the VMM, so docker returns the
        hypervisor's own command line -- and hands the caller the host's
        memory size, its cpu count and a set of internal paths, none of
        which is theirs and none of which is what they asked for.

        So the question is put inside the container instead, as the CRI
        driver already does. It needs `ps` in the image, and says so when
        there is none rather than returning an empty list of processes.
        """
        with docker_utils.docker_client() as docker:
            if self._runtime_of(container) == 'runc':
                if ps_args is None or ps_args == 'None':
                    return docker.top(container.container_id)
                return docker.top(container.container_id, ps_args)
            argv = (['ps'] + ps_args.split()
                    if ps_args and ps_args != 'None' else ['ps', '-ef'])
            created = docker.exec_create(container.container_id, argv,
                                         stdout=True, stderr=True, tty=False)
            output = docker.exec_start(created['Id'], False, False, False)
            state = docker.exec_inspect(created['Id'])
            text = output.decode('utf-8', 'replace') if output else ''
            if state.get('ExitCode'):
                raise exception.Invalid(_(
                    'Could not list processes in the container: %s')
                    % (text.strip()[:200] or 'ps is not in the image'))
            return driver.process_table(text)

    def _runtime_of(self, container):
        """Which runtime this container was actually given."""
        if not self._is_runtime_supported():
            return 'runc'
        return container.runtime or CONF.container_runtime

    @check_container_id
    @wrap_docker_error
    def get_archive(self, context, container, path):
        with docker_utils.docker_client() as docker:
            try:
                stream, stat = docker.get_archive(
                    container.container_id, path)
                if isinstance(stream, types.GeneratorType):
                    filedata = ''.encode("latin-1").join(stream)
                else:
                    filedata = stream.read()
                return filedata, stat
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    raise exception.Invalid(_("%s") % str(api_error))
                raise

    @check_container_id
    @wrap_docker_error
    def put_archive(self, context, container, path, data):
        with docker_utils.docker_client() as docker:
            try:
                docker.put_archive(container.container_id, path, data)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    raise exception.Invalid(_("%s") % str(api_error))
                raise

    @check_container_id
    @wrap_docker_error
    def stats(self, context, container):
        with docker_utils.docker_client() as docker:
            res = docker.stats(container.container_id, decode=False,
                               stream=False)

            cpu_usage = res['cpu_stats']['cpu_usage']['total_usage']
            system_cpu_usage = res['cpu_stats']['system_cpu_usage']
            previous = res.get('precpu_stats') or {}
            cpu_percent = driver.cpu_percent(
                cpu_usage,
                (previous.get('cpu_usage') or {}).get('total_usage'),
                system_cpu_usage,
                previous.get('system_cpu_usage'),
                res['cpu_stats'].get('online_cpus'))

            # Subtract the Cache Usage from total memory Usage
            mem_usage = (res['memory_stats']['usage']
                         - _cache_usage(res['memory_stats']))
            mem_usage = mem_usage / 1024 / 1024
            mem_limit = res['memory_stats']['limit'] / 1024 / 1024
            mem_percent = float(mem_usage) / float(mem_limit) * 100

            # A VM runtime reports the entries with zero in them rather
            # than leaving them out, and a zero here reads as "the disk was
            # idle" -- a claim nobody measured. Told apart by whether any
            # entry carries a device: real accounting names the device it
            # came from, the placeholder is major 0 minor 0.
            blk_stats = res['blkio_stats']['io_service_bytes_recursive']
            io_read = io_write = None
            measured = [i for i in (blk_stats or [])
                        if i.get('major') or i.get('minor')]
            if measured:
                io_read = io_write = 0
                for item in measured:
                    if item['op'].lower() == 'read':
                        io_read = io_read + item['value']
                    if item['op'].lower() == 'write':
                        io_write = io_write + item['value']

            # Note(hongbin): CNI network won't have this key
            net_stats = res.get('networks', {})
            net_rxb = 0
            net_txb = 0
            for k, v in net_stats.items():
                net_rxb = net_rxb + v['rx_bytes']
                net_txb = net_txb + v['tx_bytes']

            block_io = (driver.NO_VALUE_PAIR if io_read is None
                        else str(io_read) + "/" + str(io_write))
            stats = {"CONTAINER": container.name,
                     "CPU %": cpu_percent,
                     "MEM USAGE(MiB)": mem_usage,
                     "MEM LIMIT(MiB)": mem_limit,
                     "MEM %": mem_percent,
                     "BLOCK I/O(B)": block_io,
                     "NET I/O(B)": str(net_rxb) + "/" + str(net_txb)}
            return stats

    @check_container_id
    @wrap_docker_error
    def commit(self, context, container, repository=None, tag=None):
        with docker_utils.docker_client() as docker:
            repository = str(repository)
            if tag is None or tag == "None":
                return docker.commit(container.container_id, repository)
            else:
                tag = str(tag)
                return docker.commit(container.container_id, repository, tag)

    def _encode_utf8(self, value):
        return value.encode('utf-8')

    def get_container_name(self, container):
        return consts.NAME_PREFIX + container.uuid

    def get_host_info(self):
        with docker_utils.docker_client() as docker:
            info = docker.info()
            total = info['Containers']
            paused = info['ContainersPaused']
            running = info['ContainersRunning']
            stopped = info['ContainersStopped']
            cpus = info['NCPU']
            architecture = info['Architecture']
            os_type = info['OSType']
            os = info['OperatingSystem']
            kernel_version = info['KernelVersion']
            labels = {}
            slabels = info['Labels']
            if slabels:
                for slabel in slabels:
                    kv = slabel.split("=")
                    label = {kv[0]: kv[1]}
                    labels.update(label)
            runtimes = []
            if 'Runtimes' in info:
                for key in info['Runtimes']:
                    runtimes.append(key)
            else:
                runtimes = ['runc']
            docker_root_dir = info['DockerRootDir']

            return {'total_containers': total,
                    'running_containers': running,
                    'paused_containers': paused,
                    'stopped_containers': stopped,
                    'cpus': cpus,
                    'architecture': architecture,
                    'os_type': os_type,
                    'os': os,
                    'kernel_version': kernel_version,
                    'labels': labels,
                    'runtimes': runtimes,
                    'docker_root_dir': docker_root_dir}

    def get_total_disk_for_container(self):
        try:
            disk_usage = psutil.disk_usage(self.docker_root_dir)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            LOG.warning('Docker data root doesnot exist.')
            # give another try with system root
            disk_usage = psutil.disk_usage('/')
        total_disk = disk_usage.total / 1024 ** 3
        # TODO(hongbin): deprecate reserve_disk_for_image in flavor of
        # reserved_host_disk_mb
        return (int(total_disk),
                int(total_disk * CONF.compute.reserve_disk_for_image))

    def add_security_group(self, context, container, security_group):

        with docker_utils.docker_client() as docker:
            network_driver = zun_network.driver(context=context,
                                                docker_api=docker)
            network_driver.add_security_groups_to_ports(container,
                                                        [security_group])

    def remove_security_group(self, context, container, security_group):

        with docker_utils.docker_client() as docker:
            network_driver = zun_network.driver(context=context,
                                                docker_api=docker)
            network_driver.remove_security_groups_from_ports(container,
                                                             [security_group])

    def get_available_nodes(self):
        return [self._host.get_hostname()]

    def get_available_resources(self):
        data = super(DockerDriver, self).get_available_resources()

        info = self.get_host_info()
        data['total_containers'] = info['total_containers']
        data['running_containers'] = info['running_containers']
        data['paused_containers'] = info['paused_containers']
        data['stopped_containers'] = info['stopped_containers']
        data['cpus'] = info['cpus']
        data['architecture'] = info['architecture']
        data['os_type'] = info['os_type']
        data['os'] = info['os']
        data['kernel_version'] = info['kernel_version']
        data['labels'] = info['labels']
        data['runtimes'] = info['runtimes']

        return data

    @wrap_docker_error
    def _refuse_hotplug_on_a_vm(self, container, verb):
        """Refuse to change a running sandbox's interfaces, and say why.

        Under a VM runtime the container's interfaces are the guest's,
        fixed when the sandbox booted. docker adds or removes the veth
        in the netns on the host and reports success, but nothing
        crosses into the guest: measured on kata, the container keeps
        exactly the interfaces and addresses it had, while the API and
        `inspect` both say the network is attached. A tenant told that
        their container joined a network it cannot reach is worse off
        than one who was refused.

        Stopped, the same request works -- the address is applied when
        the sandbox next boots -- so the refusal names that.
        """
        if container.status != consts.RUNNING:
            return
        runtime = self._runtime_of(container)
        if runtime == 'runc':
            return
        raise exception.Invalid(_(
            "Cannot %(verb)s a network on container %(container)s while it "
            "is running: it runs under the %(runtime)s runtime, whose "
            "interfaces are fixed when its sandbox boots. Stop the "
            "container, %(verb)s the network, and start it again.")
            % {'verb': verb, 'container': container.uuid,
               'runtime': runtime})

    def network_detach(self, context, container, network):
        self._refuse_hotplug_on_a_vm(container, 'detach')
        with docker_utils.docker_client() as docker:
            network_driver = zun_network.driver(context,
                                                docker_api=docker)
            network_driver.disconnect_container_from_network(container,
                                                             network)

            # Only clear network info related to this network
            # Cannot del container.address directly which will not update
            # changed fields of the container objects as the del operate on
            # the addresses object, only base.getter will called.
            update = container.addresses
            del update[network]
            container.addresses = update
            container.save(context)

    def network_attach(self, context, container, requested_network):
        self._refuse_hotplug_on_a_vm(container, 'attach')
        with docker_utils.docker_client() as docker:
            security_group_ids = None
            if container.security_groups:
                security_group_ids = utils.get_security_group_ids(
                    context, container.security_groups)
            network_driver = zun_network.driver(context, docker_api=docker)
            network = requested_network['network']
            if network in container.addresses:
                raise exception.ZunException('Container %(container)s has '
                                             'already connected to the '
                                             'network %(network)s.'
                                             % {'container': container.uuid,
                                                'network': network})
            network_driver.get_or_create_network(context, network)
            addrs = network_driver.connect_container_to_network(
                container, requested_network,
                security_groups=security_group_ids)
            if addrs is None:
                raise exception.ZunException(_(
                    'Unexpected missing of addresses'))
            update = {}
            update[network] = addrs
            addresses = container.addresses
            addresses.update(update)
            container.addresses = addresses
            container.save(context)

    def create_network(self, context, neutron_net_id):
        with docker_utils.docker_client() as docker:
            network_driver = zun_network.driver(context, docker_api=docker)
            return network_driver.create_network(neutron_net_id)

    def delete_network(self, context, network):
        with docker_utils.docker_client() as docker:
            network_driver = zun_network.driver(context,
                                                docker_api=docker)
            network_driver.remove_network(network)

    def create_capsule(self, context, capsule, image, requested_networks,
                       requested_volumes):
        capsule = self.create(context, capsule, image, requested_networks,
                              requested_volumes)
        self.start(context, capsule)
        for container in capsule.init_containers:
            self._create_container_in_capsule(context, capsule, container,
                                              requested_networks,
                                              requested_volumes)
            self._wait_for_init_container(context, container)
            container.save(context)
        for container in capsule.containers:
            self._create_container_in_capsule(context, capsule, container,
                                              requested_networks,
                                              requested_volumes)
        return capsule

    def _create_container_in_capsule(self, context, capsule, container,
                                     requested_networks, requested_volumes):
        # pull image
        image_driver_name = container.image_driver
        repo, tag = utils.parse_image_name(container.image, image_driver_name)
        image_pull_policy = utils.get_image_pull_policy(
            container.image_pull_policy, tag)
        image, image_loaded = self.pull_image(
            context, repo, tag, image_pull_policy, image_driver_name)
        image['repo'], image['tag'] = repo, tag
        if not image_loaded:
            self.load_image(image['path'])
        if image_driver_name == 'glance':
            self.read_tar_image(image)
        if image['tag'] != tag:
            LOG.warning("The input tag is different from the tag in tar")

        # create container
        with docker_utils.docker_client() as docker:
            name = container.name
            LOG.debug('Creating container with image %(image)s name %(name)s',
                      {'image': image['image'], 'name': name})
            volmaps = requested_volumes.get(container.uuid, [])
            binds = self._get_binds(context, volmaps)
            kwargs = {
                'name': self.get_container_name(container),
                'command': container.command,
                'environment': container.environment,
                'working_dir': container.workdir,
                'labels': container.labels,
                'tty': container.tty,
                'stdin_open': container.interactive,
                'entrypoint': container.entrypoint,
            }
            # Same as the other create path: an unset field leaves the
            # image's USER in charge, and '' would not.
            if container.user:
                kwargs['user'] = container.user

            host_config = {}
            host_config['privileged'] = container.privileged
            self._apply_security_context(container, kwargs, host_config)
            host_config['binds'] = binds
            kwargs['volumes'] = [b['bind'] for b in binds.values()]
            host_config['network_mode'] = 'container:%s' % capsule.container_id
            # TODO(hongbin): Uncomment this after docker-py add support for
            # container mode for pid namespace.
            # host_config['pid_mode'] = 'container:%s' % capsule.container_id
            host_config['ipc_mode'] = 'container:%s' % capsule.container_id
            if container.auto_remove:
                host_config['auto_remove'] = container.auto_remove
            if container.memory is not None:
                host_config['mem_limit'] = str(container.memory) + 'M'
            if container.cpu is not None:
                host_config['cpu_shares'] = int(1024 * container.cpu)
            if container.restart_policy:
                count = int(container.restart_policy['MaximumRetryCount'])
                name = container.restart_policy['Name']
                host_config['restart_policy'] = {'Name': name,
                                                 'MaximumRetryCount': count}

            if container.disk:
                disk_size = str(container.disk) + 'G'
                host_config['storage_opt'] = {'size': disk_size}
            # The time unit in docker of heath checking is us, and the unit
            # of interval and timeout is seconds.
            if container.healthcheck:
                healthcheck = {}
                healthcheck['test'] = container.healthcheck.get('test', '')
                interval = container.healthcheck.get('interval', 0)
                healthcheck['interval'] = interval * 10 ** 9
                healthcheck['retries'] = int(container.healthcheck.
                                             get('retries', 0))
                timeout = container.healthcheck.get('timeout', 0)
                healthcheck['timeout'] = timeout * 10 ** 9
                kwargs['healthcheck'] = healthcheck

            kwargs['host_config'] = docker.create_host_config(**host_config)
            if image['tag']:
                image_repo = image['repo'] + ":" + image['tag']
            else:
                image_repo = image['repo']
            response = docker.create_container(image_repo, **kwargs)
            container.container_id = response['Id']
            docker.start(container.container_id)

            response = docker.inspect_container(container.container_id)
            self._populate_container(container, response, force=True)
            container.save(context)

    def _wait_for_init_container(self, context, container, timeout=3600):
        def retry_if_result_is_false(result):
            return result is False

        def check_init_container_stopped():
            status = self.show(context, container).status
            if status == consts.STOPPED:
                return True
            elif status == consts.RUNNING:
                return False
            else:
                raise exception.ZunException(
                    _("Container has unexpected status: %s") % status)

        r = tenacity.Retrying(
            stop=tenacity.stop_after_delay(timeout),
            wait=tenacity.wait_exponential(),
            retry=tenacity.retry_if_result(retry_if_result_is_false))
        r.call(check_init_container_stopped)

    def delete_capsule(self, context, capsule, force):
        merged_containers = (
            list(capsule.containers)
            + list(capsule.init_containers)
        )
        for container in merged_containers:
            self._delete_container_in_capsule(context, capsule, container,
                                              force)
        with docker_utils.docker_client() as docker:
            try:
                docker.stop(capsule.container_id)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    pass
        self.delete(context, capsule, force)

    def _delete_container_in_capsule(self, context, capsule, container, force):
        if not container.container_id:
            return

        with docker_utils.docker_client() as docker:
            try:
                docker.stop(container.container_id)
                docker.remove_container(container.container_id,
                                        force=force)
            except errors.APIError as api_error:
                if is_not_found(api_error):
                    return
                if is_not_connected(api_error):
                    return
                raise
