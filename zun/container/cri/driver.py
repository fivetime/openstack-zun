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
import os
import time

import grpc
import shlex
import signal
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import fileutils
from oslo_utils import timeutils
import tenacity

from zun.common import consts
from zun.common import context as zun_context
from zun.common import exception
from zun.common.i18n import _
from zun.common import utils
import zun.conf
from zun.container import driver
from zun.container import orphan
from zun.container.cri import resources as cri_resources
from zun.criapi import api_pb2
from zun.criapi import api_pb2_grpc
from zun.criapi import tasks_pb2
from zun.criapi import tasks_pb2_grpc
from zun.image import driver as img_driver
from zun.network import neutron
from zun.network import os_vif_util
from zun import objects


CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)


# DNS_SEARCHES_ANNOTATION carries the resolver search list from whoever created
# the capsule. It is an annotation rather than a field because only the creator
# knows it — for a Kubernetes provider it is derived from the pod's namespace,
# which means nothing here.
DNS_SEARCHES_ANNOTATION = 'knaas.io/dns-searches'

# DNS_SERVERS_ANNOTATION lets the creator name the resolver, overriding the
# subnet's. A Kubernetes provider needs this: the tenant's own resolver is the
# only one that answers with the names their manifests use, and it is not the
# subnet-wide one.
DNS_SERVERS_ANNOTATION = 'knaas.io/dns-nameservers'

def _security_context(container):
    """The securityContext this container was created with.

    Carried in the healthcheck column beside the probes, which is where the API
    puts it (capsules.py) — the same column, for the same reason: a new one
    means a migration.

    ⚠️ Not the capsule's annotations keyed by container name, which was the
    first attempt. The API overwrites a capsule container's name with a
    generated one, so the key never matched and every container ran as root
    with a writable root filesystem, silently.
    """
    return (container.healthcheck or {}).get('k8s_security_context') or {}


def _linux_security_context(container):
    """Build the CRI security context for one container.

    Only what was asked for is set. A field left out means "whatever the
    runtime does by default", which is what an unset securityContext field
    means in Kubernetes too.
    """
    sc = _security_context(container)
    kwargs = {'privileged': container.privileged}

    if sc.get('runAsUser') is not None:
        kwargs['run_as_user'] = api_pb2.Int64Value(value=int(sc['runAsUser']))
    if sc.get('runAsGroup') is not None:
        kwargs['run_as_group'] = api_pb2.Int64Value(value=int(sc['runAsGroup']))
    if sc.get('fsGroup') is not None:
        # The other half of fsGroup. Chowning the volume to the group does
        # nothing unless the process is IN that group; a kubelet adds it to
        # the container's supplemental groups, and so does this. Without it
        # the volume mounts, carries the right ownership, and still cannot be
        # written -- from inside the pod that is indistinguishable from a
        # broken volume.
        kwargs['supplemental_groups'] = [int(sc['fsGroup'])]
    if sc.get('readOnlyRootFilesystem'):
        kwargs['readonly_rootfs'] = True
    # allowPrivilegeEscalation: false is no_new_privs: true. Named the other way
    # round in each system, which is worth stating rather than trusting.
    if sc.get('allowPrivilegeEscalation') is False:
        kwargs['no_new_privs'] = True

    caps = sc.get('capabilities') or {}
    if caps.get('add') or caps.get('drop'):
        # Second line of defence behind the API's validation: even if a
        # forbidden capability reached the stored spec (a direct DB write, a
        # future regression in the API check), it does not reach the runtime.
        # Dropping is never restricted.
        allowed = {c.upper() for c in CONF.allowed_capabilities}
        add = [c for c in (caps.get('add') or []) if str(c).upper() in allowed]
        dropped = [c for c in (caps.get('add') or []) if str(c).upper() not in allowed]
        if dropped:
            LOG.warning("refusing capabilities %s on container %s; this host "
                        "allows adding only %s",
                        dropped, container.container_id,
                        sorted(allowed) or '(none)')
        kwargs['capabilities'] = api_pb2.Capability(
            add_capabilities=add,
            drop_capabilities=list(caps.get('drop') or []))

    profile = sc.get('seccompProfile')
    if profile:
        kwargs['seccomp'] = _seccomp_profile(profile)

    return api_pb2.LinuxContainerSecurityContext(**kwargs)


def _seccomp_profile(profile):
    kind = profile.get('type')
    if kind == 'RuntimeDefault':
        return api_pb2.SecurityProfile(
            profile_type=api_pb2.SecurityProfile.RuntimeDefault)
    if kind == 'Localhost':
        # Refused at the API; should never arrive. If it does (direct DB
        # write), do not hand the runtime a tenant-named host path -- fall
        # back to the runtime default, the stricter of the two.
        LOG.warning("ignoring unsupported Localhost seccomp profile; "
                    "using the runtime default")
        return api_pb2.SecurityProfile(
            profile_type=api_pb2.SecurityProfile.RuntimeDefault)
    # Unconfined, or anything unrecognised: say so rather than guessing at a
    # stricter profile the workload was not built for.
    return api_pb2.SecurityProfile(
        profile_type=api_pb2.SecurityProfile.Unconfined)


def _annotation_list(subject, key):
    raw = (getattr(subject, 'annotations', None) or {}).get(key)
    if not raw:
        return []
    return [s for s in (part.strip() for part in raw.split(',')) if s]


def _own_list(subject, field):
    """A container's own resolvers, which a capsule does not have.

    The sandbox is built from a capsule on one path and from a container
    on the other, and only the container carries `dns`/`dns_search` -- the
    fields the Container API grew for them. Read from the annotations
    alone, a container's request was accepted and then dropped: the
    tenant asked for a resolver, got no error, and found the host's
    resolv.conf inside their container. Silently honouring nothing is
    worse than refusing, because nothing tells them to look.
    """
    return list(getattr(subject, field, None) or [])


def _dns_searches(subject):
    return (_own_list(subject, 'dns_search')
            or _annotation_list(subject, DNS_SEARCHES_ANNOTATION))


def _dns_servers(subject):
    return (_own_list(subject, 'dns')
            or _annotation_list(subject, DNS_SERVERS_ANNOTATION))


def _signal_number(signal_name):
    """Turn what the API was given into a number the task service takes.

    The Container API accepts a signal however the caller wrote it -- SIGTERM,
    TERM, 15 -- because docker-py did. Defaulting to SIGKILL matches the
    docker driver, whose kill with no signal kills.
    """
    if signal_name is None or str(signal_name) in ('None', ''):
        return int(signal.SIGKILL)
    text = str(signal_name).strip().upper()
    if text.isdigit():
        return int(text)
    if not text.startswith('SIG'):
        text = 'SIG' + text
    try:
        return int(getattr(signal, text))
    except AttributeError:
        raise exception.Invalid(_('%s is not a signal this host knows')
                                % signal_name)


def _restart_count(container):
    """How many times a probe has had this container replaced."""
    state = (container.healthcheck or {}).get('k8s_probe_state') or {}
    try:
        return int(state.get('restarts') or 0)
    except (TypeError, ValueError):
        return 0


class CriDriver(driver.BaseDriver, driver.ContainerDriver,
                driver.CapsuleDriver):
    """Implementation of container drivers for CRI runtime.

    Serves both shapes on one runtime. A capsule is a sandbox holding several
    containers; a container is a sandbox holding one. That is not a trick to
    reuse code -- it is what a container IS under the CRI, which has no
    concept of a container outside a sandbox. So the machinery is the same
    machinery, and what differs is only how many containers go in and who
    asked.

    Which matters beyond tidiness: both shapes then share one containerd, one
    Kata, one set of VMMs and one resource account, instead of a second daemon
    keeping its own images and its own sandboxes beside the first.
    """

    # TODO(hongbin): define a list of capabilities of this driver.
    capabilities = {}

    def __init__(self):
        super(CriDriver, self).__init__()
        channel = grpc.insecure_channel(
            'unix:///run/containerd/containerd.sock')
        self.runtime_stub = api_pb2_grpc.RuntimeServiceStub(channel)
        self.image_stub = api_pb2_grpc.ImageServiceStub(channel)
        # containerd's own task service, on the same socket. The CRI is a view
        # of containerd, not the whole of it: pausing a task and sending it a
        # signal exist here and have no CRI call at all. Reaching past the CRI
        # is a deliberate exception, kept to the calls that are only here --
        # anything the CRI does serve is served through the CRI.
        self.task_stub = tasks_pb2_grpc.TasksStub(channel)
        # Fetching an image has never depended on which runtime will run it,
        # so the image drivers are the same ones every other container driver
        # loads. Only the Container API path uses them; a capsule pulls
        # through the runtime.
        self.image_drivers = {}
        for driver_name in CONF.image_driver_list:
            self.image_drivers[driver_name] = img_driver.load_image_driver(
                driver_name)

    def create_capsule(self, context, capsule, image, requested_networks,
                       requested_volumes):

        self._create_pod_sandbox(context, capsule, requested_networks)

        # TODO(hongbin): handle init containers
        for container in capsule.init_containers:
            self._create_container(context, capsule, container,
                                   requested_volumes)
            self._wait_for_init_container(context, container)
            container.save(context)

        for container in capsule.containers:
            self._create_container(context, capsule, container,
                                   requested_volumes)
            container.status = consts.RUNNING
            container.save(context)

        capsule.status = consts.RUNNING
        return capsule

    def _create_pod_sandbox(self, context, capsule, requested_networks,
                            labels=None):
        runtime = capsule.runtime or CONF.container_runtime
        if runtime == "runc":
            # pass "" to specify the default runtime which is runc
            runtime = ""

        dns_servers = self._write_cni_metadata(context, capsule,
                                               requested_networks)
        # What the creator asked for wins over the subnet's: the subnet serves
        # every capsule on it, while this is the resolver for one capsule's
        # tenant.
        servers = _dns_servers(capsule) or dns_servers
        sandbox_config = self._get_sandbox_config(
            capsule, servers, _dns_searches(capsule), labels=labels)
        sandbox_resp = self.runtime_stub.RunPodSandbox(
            api_pb2.RunPodSandboxRequest(
                config=sandbox_config,
                runtime_handler=runtime,
            )
        )
        LOG.debug("podsandbox is created: %s", sandbox_resp)
        capsule.container_id = sandbox_resp.pod_sandbox_id

    def _get_sandbox_config(self, capsule, dns_servers=None,
                            dns_searches=None, labels=None):
        config = api_pb2.PodSandboxConfig(
            metadata=api_pb2.PodSandboxMetadata(
                name=capsule.uuid, namespace="default", uid=capsule.uuid
            ),
            # Without a log directory the runtime discards a container's output
            # entirely: there is no stream to attach to after the fact and
            # nothing on disk, so the logs API has nothing to serve.
            log_directory=self._log_directory(),
            # ⚠️ Without a cgroup parent the runtime has nowhere to hang the
            # sandbox's own accounting, and PodSandboxStats -- the only call
            # that carries network counters -- fails outright with "sandbox
            # has no cgroup parent" before it gets as far as reading them
            # (containerd internal/cri/server/podsandbox/sandbox_stats_linux
            # .go, which reads this very field back). Measured: every running
            # sandbox on the node answered that error, and crictl had been
            # warning "cgroup_parent is not set" all along.
            linux=api_pb2.LinuxPodSandboxConfig(
                cgroup_parent=self._sandbox_cgroup_parent(capsule)),
        )
        if labels:
            # What ties a sandbox back to whoever owns it. A container records
            # its container id, not its sandbox id -- there is one field and
            # the container id has the stronger claim -- so the sandbox has to
            # be findable some other way.
            config.labels.update(labels)
        if dns_servers:
            config.dns_config.servers.extend(dns_servers)
        if dns_searches:
            # Without these a container resolves only fully qualified names.
            # Applications written for Kubernetes routinely use the short form
            # of a service name and rely on the search list to complete it, so
            # leaving it out breaks them in a way that looks like the service
            # is missing rather than like a resolver setting.
            config.dns_config.searches.extend(dns_searches)
        return config

    @staticmethod
    def _sandbox_cgroup_parent(capsule):
        """Where this sandbox's own accounting lives on the host.

        One slice per capsule under a shared parent, so a node's capsules are
        countable in one place and one capsule's usage is separable from the
        next. The name is the capsule's uuid because that is what every other
        record of it is keyed by.

        ⚠️ On a kata capsule the numbers under here describe the virtual
        machine -- its vcpu time and the memory the VMM holds -- not what runs
        inside the guest. That is the right figure for a node's accounting and
        the wrong one for a container's, which is why container stats keep
        coming from ContainerStats rather than from here.
        """
        # ⚠️ Absolute, and clean: the runtime hands this straight to the
        # cgroup library, which rejects anything not starting with "/"
        # (cgroup2 VerifyGroupPath) -- and rejects it with "invalid group
        # path", which reads like the path names something that does not
        # exist rather than like a path written the wrong way.
        return '/%s/%s' % (CONF.cri_sandbox_cgroup_parent.strip('/'),
                           capsule.uuid)

    @staticmethod
    def _log_directory():
        # Flat rather than a directory per capsule: a container's log is then
        # named by the container alone, so reading it back needs nothing but
        # the container — no lookup from a capsule id, which the object layer
        # has no getter for.
        root = CONF.cri_log_root
        fileutils.ensure_tree(root)
        return root

    @staticmethod
    def _log_path(container):
        # Relative to the sandbox's log directory; the runtime joins the two.
        return '%s.log' % container.uuid

    def _write_cni_metadata(self, context, capsule, requested_networks):
        neutron_api = neutron.NeutronAPI(context)
        security_group_ids = utils.get_security_group_ids(
            context, capsule.security_groups)
        # TODO(hongbin): handle multiple nics
        requested_network = requested_networks[0]
        network_id = requested_network['network']
        addresses, port = neutron_api.create_or_update_port(
            capsule, network_id, requested_network, consts.DEVICE_OWNER_ZUN,
            security_group_ids, set_binding_host=True)
        capsule.addresses = {network_id: addresses}

        neutron_api = neutron.NeutronAPI(zun_context.get_admin_context())
        network = neutron_api.show_network(port['network_id'])['network']
        subnets = {}
        for fixed_ip in port['fixed_ips']:
            subnet_id = fixed_ip['subnet_id']
            subnets[subnet_id] = neutron_api.show_subnet(subnet_id)['subnet']
        vif_plugin = port.get('binding:vif_type')
        vif_obj = os_vif_util.neutron_to_osvif_vif(vif_plugin, port, network,
                                                   subnets)
        state = objects.vif.VIFState(default_vif=vif_obj)
        state_dict = state.obj_to_primitive()
        capsule.cni_metadata = {consts.CNI_METADATA_VIF: state_dict}
        capsule.save(context)

        # The sandbox otherwise inherits the host's resolv.conf, which resolves
        # nothing the tenant network knows about.
        dns_servers = []
        for subnet in subnets.values():
            for nameserver in subnet.get('dns_nameservers') or []:
                if nameserver not in dns_servers:
                    dns_servers.append(nameserver)
        return dns_servers

    def _create_container(self, context, capsule, container,
                          requested_volumes, start=True, attempt=None):
        """Create one container inside a sandbox, and start it by default.

        A capsule's containers are started as they are made -- a capsule has
        no half-built state to sit in. A container created through the
        Container API does: the caller starts it separately, and only when it
        asked to run. Starting it anyway and stopping it again would boot a
        virtual machine under Kata for no reason.
        """
        # pull image
        self._pull_image(context, container)

        sandbox_config = self._get_sandbox_config(capsule)
        container_config = self._get_container_config(context, capsule, container,
                                                      requested_volumes,
                                                      attempt=attempt)
        response = self.runtime_stub.CreateContainer(
            api_pb2.CreateContainerRequest(
                pod_sandbox_id=capsule.container_id,
                config=container_config,
                sandbox_config=sandbox_config,
            )
        )

        LOG.debug("container is created: %s", response)
        container.container_id = response.container_id
        container.save(context)

        if not start:
            return

        response = self.runtime_stub.StartContainer(
            api_pb2.StartContainerRequest(
                container_id=container.container_id
            )
        )
        LOG.debug("container is started: %s", response)

    def _get_container_config(self, context, capsule, container,
                              requested_volumes, attempt=None):
        args = []
        if container.command:
            args = [str(c) for c in container.command]
        envs = []
        if container.environment:
            # KeyValue.value is bytes in the v1 runtime API, unlike the
            # v1alpha2 message this code was written against, and protobuf
            # refuses a str for it.
            envs = [api_pb2.KeyValue(key=str(k), value=str(v).encode())
                    for k, v in container.environment.items()]
        mounts = []
        if container.uuid in requested_volumes:
            req_volume = requested_volumes[container.uuid]
            mounts = self._get_mounts(context, req_volume)
        probe_mount = self._probe_helper_mount(container)
        if probe_mount is not None:
            mounts.append(probe_mount)
        working_dir = container.workdir or ""
        labels = container.labels or []

        linux_config = api_pb2.LinuxContainerConfig(
            security_context=_linux_security_context(container),
            resources=cri_resources.linux_resources(container.cpu, container.memory),
        )

        # The attempt number is what distinguishes one incarnation of a
        # container from the next, both in the runtime's own naming and in what
        # crictl shows, so a restarted container does not look like the
        # original still running.
        if attempt is None:
            attempt = _restart_count(container)

        # TODO(hongbin): add support for entrypoint
        return api_pb2.ContainerConfig(
            metadata=api_pb2.ContainerMetadata(name=container.name,
                                               attempt=attempt),
            image=api_pb2.ImageSpec(image=container.image),
            tty=container.tty,
            stdin=container.interactive,
            args=args,
            envs=envs,
            working_dir=working_dir,
            labels=labels,
            mounts=mounts,
            linux=linux_config,
            log_path=self._log_path(container),
        )

    def show_logs(self, context, container, stdout=True, stderr=True,
                  timestamps=False, tail='all', since=None):
        """Read a capsule container's logs from the file the runtime writes.

        The runtime has no API to read them back — it only writes the file it
        was told to — so this parses the CRI log format directly:

            <RFC3339Nano> <stdout|stderr> <P|F> <line>

        A partial line (P) is a line the runtime split because it was longer
        than its buffer; joining those back is what stops a long log line from
        arriving as several.
        """
        path = self._container_log_file(container)
        if not os.path.exists(path):
            # A container that has not started yet has no file, and empty
            # output says that better than an error would. Logged because the
            # other way to reach this is a request that landed on the wrong
            # node, which otherwise looks exactly like a container that printed
            # nothing.
            LOG.info("No log file %s for container %s", path, container.uuid)
            return b''

        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = None
        since = self._parse_since(since)

        lines = []
        partial = []
        with open(path, 'rb') as fd:
            for raw in fd:
                parsed = self._parse_log_line(raw)
                if parsed is None:
                    continue
                stamp, stream, is_partial, text = parsed
                if stream == 'stdout' and not stdout:
                    continue
                if stream == 'stderr' and not stderr:
                    continue
                if since is not None and stamp is not None and stamp < since:
                    continue
                partial.append(text)
                if is_partial:
                    continue
                line = b''.join(partial)
                partial = []
                if timestamps and stamp is not None:
                    line = raw.split(b' ', 1)[0] + b' ' + line
                lines.append(line)
        if partial:
            lines.append(b''.join(partial))

        if tail is not None and tail >= 0:
            lines = lines[-tail:] if tail else []
        return b''.join(lines)

    @staticmethod
    def _container_log_file(container):
        return os.path.join(CONF.cri_log_root, '%s.log' % container.uuid)

    @staticmethod
    def _parse_log_line(raw):
        """Split one CRI log line, or None if it is not one."""
        parts = raw.split(b' ', 3)
        if len(parts) < 4:
            return None
        stamp_raw, stream, tag, text = parts
        try:
            stamp = timeutils.parse_isotime(stamp_raw.decode())
        except (ValueError, UnicodeDecodeError):
            stamp = None
        return stamp, stream.decode(errors='replace'), tag.startswith(b'P'), text

    @staticmethod
    def _parse_since(since):
        if since is None or since == 'None':
            return None
        try:
            return datetime.datetime.fromtimestamp(
                int(since), tz=datetime.timezone.utc)
        except (TypeError, ValueError):
            pass
        try:
            return timeutils.parse_isotime(since)
        except ValueError:
            raise exception.InvalidValue(
                _('since must be an epoch second or an ISO 8601 time'))

    def _pull_image(self, context, container):
        self._pull_image_ref(container.image)

    def _pull_image_ref(self, ref):
        # TODO(hongbin): add support for private registry
        response = self.image_stub.PullImage(
            api_pb2.PullImageRequest(image=api_pb2.ImageSpec(image=ref)))
        LOG.debug("image is pulled: %s", response)

    def _probe_helper_mount(self, container):
        """Mount the probe helper for a container that has probes.

        A probe has to run inside the container: nothing outside reaches it,
        because the address is on the tenant network and lives inside the VM
        rather than in the sandbox's namespace on this host. So a rewritten
        httpGet becomes an exec, and an exec needs something to execute --
        which a distroless image does not have. No shell, no curl, no wget.

        Mounting the helper from this host rather than building it into the
        image or shipping it inside each capsule keeps it out of the tenant's
        way entirely: the image is untouched, the capsule carries nothing, and
        the tool is versioned with the compute node.

        Read-only, and only for containers that actually declare a probe --
        every mount is a virtiofs device to a Kata guest, so one nobody uses is
        a device nobody uses.
        """
        if not CONF.probe_helper_path:
            return None
        probes = (container.healthcheck or {}).get('k8s_probes') or {}
        if not probes:
            return None
        if not os.path.isdir(CONF.probe_helper_path):
            # Said loudly and once per container rather than failing the
            # create: a node missing the helper still runs everything whose
            # probes are plain exec, and a create refused here would read to
            # the tenant as their image being wrong.
            LOG.warning("probe_helper_path %s does not exist on this host; "
                        "containers with httpGet or tcpSocket probes will "
                        "fail them until it does",
                        CONF.probe_helper_path)
            return None
        return api_pb2.Mount(container_path=CONF.probe_helper_mount,
                             host_path=CONF.probe_helper_path,
                             readonly=True)

    def _get_mounts(self, context, volmaps):
        mounts = []
        for volume in volmaps:
            volume_driver = self._get_volume_driver(volume)
            source, destination = volume_driver.bind_mount(context, volume)
            mounts.append(api_pb2.Mount(container_path=destination,
                                        host_path=source))
        return mounts

    def _wait_for_init_container(self, context, container, timeout=3600):
        def retry_if_result_is_false(result):
            return result is False

        def check_init_container_stopped():
            status = self._show_container(context, container).status
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

    def _show_container(self, context, container):
        container_id = container.container_id
        if not container_id:
            return

        response = self.runtime_stub.ListContainers(
            api_pb2.ListContainersRequest(
                filter={'id': container_id}
            )
        )
        if not response.containers:
            raise exception.ZunException(
                "Container %s is not found in runtime" % container_id)

        container_response = response.containers[0]
        self._populate_container(container, container_response)
        return container

    def _populate_container(self, container, response):
        self._populate_container_state(container, response)

    def _populate_container_state(self, container, response):
        state = response.state
        if state == api_pb2.ContainerState.CONTAINER_CREATED:
            container.status = consts.CREATED
        elif state == api_pb2.ContainerState.CONTAINER_RUNNING:
            # ⚠️ The CRI has no paused state to report, so a frozen container
            # arrives here as running -- which is how a container that has
            # stopped doing anything gets reported as working normally, and how
            # a paused one would silently resist every attempt to resume it.
            # containerd knows; it is asked only where the answer could differ.
            if container.status == consts.PAUSED and self._still_paused(
                    container.container_id):
                return
            container.status = consts.RUNNING
            new_run = False
            if (container.status_detail or '').startswith('exit:'):
                # A recorded exit belongs to a previous run of this container
                # (a probe restart makes a new one under the same record);
                # carrying it into a running container would report the old
                # death on the living.
                container.status_detail = None
                new_run = True
            if new_run or not container.started_at:
                # started_at was only ever written at exit (_record_exit), so
                # every RUNNING container reported null -- the value exists
                # solely in the ContainerStatus response, which the listing
                # this sync reads does not carry. One extra RPC, once per run:
                # unset means never asked, a cleared exit marker means the
                # stored time belongs to the previous run.
                self._record_start(container)
        elif state == api_pb2.ContainerState.CONTAINER_EXITED:
            container.status = consts.STOPPED
            self._record_exit(container)
        elif state == api_pb2.ContainerState.CONTAINER_UNKNOWN:
            LOG.debug('State is unknown, status: %s', state)
            container.status = consts.UNKNOWN
        else:
            LOG.warning('Receive unexpected state from CRI runtime: %s', state)
            container.status = consts.UNKNOWN
            container.status_reason = "container state unknown"

    def _record_exit(self, container):
        """Record how a container exited, in status_detail as "exit:<code>".

        The listing this sync reads carries only the state, not the code --
        CRI's ListContainers response has no exit_code field -- so a stopped
        container needs one ContainerStatus call to learn it. Without this the
        record says STOPPED and nothing else, and the consumer above (kubezun)
        can only guess from the status name; it guessed 0, which is the code
        callers read as success. A Job whose command failed was judged
        Completed. Recording the code is what lets a failure look like one.

        Recorded once per run: the marker is cleared when the container runs
        again, so a re-exit re-records. A failed status call leaves the detail
        empty rather than wrong -- absent means "was never told", and the
        consumer falls back to its status-name heuristic, which is the honest
        remainder.
        """
        if (container.status_detail or '').startswith('exit:'):
            return
        try:
            resp = self.runtime_stub.ContainerStatus(
                api_pb2.ContainerStatusRequest(
                    container_id=container.container_id))
        except grpc.RpcError as e:
            LOG.warning('Could not read the exit status of %(id)s: %(err)s',
                        {'id': container.container_id, 'err': e})
            return
        st = resp.status
        container.status_detail = 'exit:%d' % st.exit_code
        # The same code the docker driver records, so that a caller sees the
        # container's exit status regardless of which backend ran it.
        container.exit_code = st.exit_code
        if st.started_at:
            # Nanoseconds since the epoch; kubezun reports it as the
            # container's startedAt, which was null for every capsule
            # container before this.
            container.started_at = datetime.datetime.utcfromtimestamp(
                st.started_at / 1e9)

    def get_available_resources(self):
        data = super(CriDriver, self).get_available_resources()
        data['runtimes'] = self._available_runtimes()
        return data

    def _available_runtimes(self):
        """The runtime handlers this node actually offers, from the runtime.

        ⚠️ Without this the node reports an empty list, and the scheduler's
        RuntimeFilter fails every host for any capsule that names a runtime
        -- measured: the first capsule to carry one died NoValidHost on a
        cloud whose every node had the handler. The truth is the runtime's
        own config, which CRI Status carries; a zun.conf list would drift
        from it the day an operator edits containerd and not zun.
        """
        try:
            resp = self.runtime_stub.Status(api_pb2.StatusRequest())
        except grpc.RpcError as e:
            LOG.warning('Could not list runtime handlers: %s', e)
            return []
        names = sorted({h.name for h in resp.runtime_handlers if h.name})
        if names:
            return names
        # An older runtime that predates handler reporting: fall back to the
        # one runtime this host is configured to use, which is the only one
        # anything could ask it for anyway.
        return [CONF.container_runtime] if CONF.container_runtime else []

    def _record_start(self, container):
        """Read when this run of the container started, from the runtime.

        Failure leaves the field as it was: for a first run that is null
        ("was never told"), for a restart it is the previous run's time --
        stale but real, and the next sync retries via the same conditions.
        """
        try:
            resp = self.runtime_stub.ContainerStatus(
                api_pb2.ContainerStatusRequest(
                    container_id=container.container_id))
        except grpc.RpcError as e:
            LOG.warning('Could not read the start time of %(id)s: %(err)s',
                        {'id': container.container_id, 'err': e})
            return
        if resp.status.started_at:
            container.started_at = datetime.datetime.utcfromtimestamp(
                resp.status.started_at / 1e9)

    def _still_paused(self, container_id):
        status = self._task_status(container_id)
        if status is None:
            # Unreadable, so unchanged: reporting it running on the strength of
            # a failed query would resume it in the record and nowhere else.
            return True
        return status in (tasks_pb2.Process.PAUSED,
                          tasks_pb2.Process.PAUSING)

    @staticmethod
    def _as_argv(command):
        """Take the command however the caller wrote it.

        The capsule API splits the string before it gets here and the docker
        driver accepted one, so nothing upstream ever had to. The CRI takes a
        list, and handing a string to a repeated field is accepted silently
        one character at a time: `id` becomes ['i', 'd'], and the runtime
        reports that the file `i` was not found.
        """
        if isinstance(command, str):
            return shlex.split(command)
        return list(command or [])

    def _exec_in_container(self, container_id, cmd, timeout):
        """Run a command inside a container and return its exit code.

        This is the only way to observe a capsule from the outside. Nothing on
        the compute host can reach a capsule's address -- it lives on the
        tenant's OVN network, and a kata sandbox's namespace holds only a tap
        device -- so a probe has to run where the application is.
        """
        response = self.runtime_stub.ExecSync(
            api_pb2.ExecSyncRequest(
                container_id=container_id,
                cmd=self._as_argv(cmd),
                timeout=timeout,
            )
        )
        return response.exit_code, response.stdout, response.stderr

    def execute_create(self, context, container, command, interactive=False):
        """Prepare an exec. The CRI has no separate create step.

        The runtime's synchronous exec takes the command and the container in
        one call, so there is nothing to create and nothing to keep; the
        container id is handed back as the handle execute_run expects.
        """
        if interactive:
            return self._create_streaming_exec(container, command)
        return container.container_id

    def _create_streaming_exec(self, container, command):
        """Ask the runtime for a URL an interactive session can attach to.

        Exec, not ExecSync. ExecSync runs the command to completion and hands
        back everything at once, which is the right shape for a probe and the
        wrong one for a terminal: there is nothing to type into. Exec returns a
        URL served by the runtime's own streaming server, which already speaks
        the protocol kubectl expects -- five channels over one websocket,
        stdin, stdout, stderr, error and resize.

        Resize needs no call of its own here. The CRI has no resize RPC: a
        terminal's new size travels on the stream's fifth channel and the
        runtime applies it. Anything that proxies the stream byte for byte
        carries it for free.

        The URL is where the runtime put it -- loopback on this node, a random
        port -- so it is only useful to something running here. That is the
        whole reason the proxy exists.
        """
        response = self.runtime_stub.Exec(api_pb2.ExecRequest(
            container_id=container.container_id,
            cmd=self._as_argv(command),
            tty=True,
            stdin=True,
            stdout=True,
            stderr=False,  # A tty merges stderr into stdout; asking for both
                           # is refused by the runtime.
        ))
        if not response.url:
            raise exception.ZunException(_(
                'the runtime returned no streaming url for an interactive '
                'exec'))
        return response.url

    def exec_stream_url(self, exec_id):
        """Where an interactive session made by execute_create is served.

        For this driver the handle IS the url: the runtime hands one back and
        there is nothing else to remember it by.
        """
        return exec_id

    def execute_resize(self, exec_id, height, width):
        """Not a call here: see _create_streaming_exec.

        A terminal resize travels on the exec stream itself, which the runtime
        reads and applies. Answering with an error rather than silently doing
        nothing, because a caller reaching this has a resize that would
        otherwise be lost with no sign of it.
        """
        raise exception.OperationNotSupported(message=_(
            'a terminal resize travels on the exec stream and needs no '
            'separate call'))

    def execute_run(self, exec_id, command):
        # Bounded well below the RPC reply timeout. A command that outlives
        # that — `sh` with nothing on stdin is enough — leaves the caller with
        # a server error sixty seconds later instead of being told it ran too
        # long.
        timeout = CONF.cri_exec_timeout
        try:
            exit_code, out, err = self._exec_in_container(
                exec_id, command, timeout)
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise exception.Invalid(
                    _('Command did not finish within %d seconds') % timeout)
            raise
        # stderr is included because a caller running a command wants to see
        # why it failed, and there is no second stream to send it down.
        output = (out or b'') + (err or b'')
        return output, exit_code

    def _run_probe(self, container, probe):
        """Run one probe. Returns True when it succeeds.

        Only exec probes arrive here: the caller rewrites httpGet, tcpSocket
        and gRPC into an exec against the container itself, because a probe
        cannot reach the container from anywhere else.
        """
        exec_action = (probe or {}).get('exec') or {}
        cmd = exec_action.get('command')
        if not cmd:
            LOG.warning("Probe for container %s has no exec command; "
                        "treating it as failed rather than healthy",
                        container.container_id)
            return False

        # The probe's own timeout bounds the tenant's command; the deadline on
        # the exec has to be larger, or the two race. A rewritten network probe
        # already tells its tool to wait timeoutSeconds, so an exec cut off at
        # exactly that leaves no room for the shell to start and reports a
        # failure for a container that answered — intermittently, which is
        # worse than never.
        timeout = int(probe.get('timeoutSeconds') or 1) + CONF.probe_exec_overhead
        try:
            exit_code, _out, err = self._exec_in_container(
                container.container_id, list(cmd), timeout)
        except Exception as e:
            # An unreadable probe is a failed probe: reporting healthy here
            # would hide exactly the failure the probe exists to catch.
            LOG.debug("Probe for container %(id)s could not run: %(err)s",
                      {'id': container.container_id, 'err': e})
            return False

        if exit_code != 0:
            LOG.debug("Probe for container %(id)s failed with %(code)s: "
                      "%(err)s",
                      {'id': container.container_id, 'code': exit_code,
                       'err': err[:200] if err else ''})
        return exit_code == 0

    def reap_orphans(self, context, min_age, dry_run=False):
        """Reap sandboxes we labelled whose owner row is gone.

        ⚠️ The real authority on this path is Kubernetes, and it is not
        reachable from here -- kubezun holds that answer and sweeps against
        it. What is reachable is the owner label this driver stamps on every
        sandbox it creates, plus the rows that made them. A sandbox carrying
        our label whose row no longer exists is ours to remove; anything
        without the label belongs to the kubelet and is never touched.

        Because the two sweeps can overlap, this one is off unless an
        operator turns it on ([compute] reclaim_orphan_containers = all).
        """
        sandboxes = []
        try:
            response = self.runtime_stub.ListPodSandbox(
                api_pb2.ListPodSandboxRequest())
        except grpc.RpcError as e:
            LOG.debug('Orphan sweep skipped, cannot list sandboxes: %s', e)
            return (0, 0, 0)

        now_ns = time.time() * 1e9
        for item in response.items:
            owner = (item.labels or {}).get(self.OWNER_LABEL)
            if not owner:
                # The kubelet's own. Not ours to judge.
                continue
            age = None
            if item.created_at:
                age = (now_ns - item.created_at) / 1e9
            obj = orphan.RuntimeObject(item.id, age, label=owner)
            obj.owner_uuid = owner
            sandboxes.append(obj)

        known = set()
        for row in objects.Capsule.list(context):
            known.add(row.uuid)
        for row in objects.Container.list(context):
            known.add(row.uuid)

        def is_claimed(obj):
            return obj.owner_uuid in known

        def remove(obj):
            self.runtime_stub.RemovePodSandbox(
                api_pb2.RemovePodSandboxRequest(pod_sandbox_id=obj.ident))

        return orphan.sweep('k8s.io', sandboxes, is_claimed, remove, min_age,
                            dry_run=dry_run)

    def update_containers_states(self, context, capsules, manager):
        """Refresh capsule state from what the runtime actually reports.

        Without this a capsule keeps the status it had when the last operation
        finished: a container that died is still reported Running, so nothing
        above -- neither Zun nor a virtual-kubelet reading these statuses --
        ever learns the workload is gone.
        """
        for capsule in capsules:
            if capsule.status in (consts.CREATING, consts.DELETING,
                                  consts.DELETED):
                # Mid-operation; the operation itself owns the status.
                continue

            # A capsule holds containers; a container created through the
            # Container API is its own only member. Both reach here now that
            # this host serves both and the periodic sweep hands over
            # everything it finds. ⚠️ Assuming capsules did not merely skip
            # containers -- it raised, and one raise ends the whole sweep, so
            # every capsule on the node stopped being reconciled too.
            members = getattr(capsule, 'containers', None)
            aggregate = members is not None
            if not aggregate:
                members = [capsule]

            changed = False
            running = 0
            counted = 0
            for container in members:
                old_status = container.status
                old_started = container.started_at
                try:
                    self._show_container(context, container)
                except exception.ZunException:
                    # The runtime does not know this container. That reads as
                    # "gone", but it is also what a container mid-creation and
                    # a runtime still recovering look like, and calling it gone
                    # marks the capsule stopped, terminates the pod and has the
                    # workload rebuilt for nothing. A container that really has
                    # gone is caught by capsule deletion and by the orphan
                    # sweep, both of which check against Kubernetes rather than
                    # against one runtime query.
                    LOG.debug("Runtime does not report container %s; leaving "
                              "its status alone", container.container_id)
                    continue
                except Exception as e:
                    LOG.warning("Could not read state of container %(id)s: "
                                "%(err)s",
                                {'id': container.container_id, 'err': e})
                    continue

                counted += 1
                if container.status == consts.RUNNING:
                    running += 1
                if container.status != old_status:
                    LOG.info("Container %(id)s changed from %(old)s to "
                             "%(new)s",
                             {'id': container.container_id,
                              'old': old_status, 'new': container.status})
                    container.save(context)
                    changed = True
                elif container.started_at != old_started:
                    # Learned without a state change -- a running container
                    # whose start time was read this round. Unsaved it would
                    # be re-read and re-lost every sweep.
                    container.save(context)

            if not counted or not aggregate:
                # Nothing to roll up: a lone container already saved itself.
                continue

            # The capsule is only running while all of its containers are: a
            # capsule reported Running with a dead container inside it is the
            # failure this method exists to surface.
            status = consts.RUNNING if running == counted else consts.STOPPED
            if capsule.status != status:
                LOG.info("Capsule %(uuid)s changed from %(old)s to %(new)s",
                         {'uuid': capsule.uuid, 'old': capsule.status,
                          'new': status})
                capsule.status = status
                changed = True
            if changed:
                capsule.save(context)

    def check_probes(self, context, capsules):
        """Run any probe that is due.

        Separate from the state sync because the two answer to different
        clocks: state is reconciled on the service's own interval, while a
        probe has the period its author asked for. Riding the state sync meant
        every probe ran at that interval whatever periodSeconds said, so a
        five-second liveness check took a minute to notice anything.
        """
        for capsule in capsules:
            if capsule.status != consts.RUNNING:
                continue
            for container in capsule.containers:
                if container.status != consts.RUNNING:
                    continue
                try:
                    self._check_probes(context, capsule, container)
                except Exception as e:
                    # One container's probe must not stop the rest from running.
                    LOG.warning("Probes for container %(id)s failed to run: "
                                "%(err)s",
                                {'id': container.container_id, 'err': e})

    def _check_probes(self, context, capsule, container):
        """Run a running container's probes and act on the result.

        Returns True when something about the container changed.

        Probe state is kept in the container's healthcheck field alongside the
        probe definitions, so a restart of zun-compute loses only the counters
        -- a probe that is still failing simply fails again.
        """
        probes = (container.healthcheck or {}).get('k8s_probes') or {}
        if not probes:
            return False

        # Copied, not referenced: mutating the dict inside healthcheck would
        # make the save below compare a value against itself, find no change,
        # and never persist anything.
        state = dict((container.healthcheck or {}).get('k8s_probe_state') or {})
        changed = False

        # A startup probe gates the other two: until it passes, a slow-starting
        # application must not be restarted for failing a liveness check it was
        # never given time to satisfy.
        now = time.time()

        startup = probes.get('startupProbe')
        if startup and not state.get('startup_passed'):
            if not self._probe_due(state, 'startup', startup, container, now):
                return False
            self._schedule_next(state, 'startup', startup, now)
            if self._probe_passed(container, startup, state, 'startup'):
                state['startup_passed'] = True
            else:
                if self._probe_failed_enough(startup, state, 'startup'):
                    LOG.info("Startup probe for container %s never passed; "
                             "restarting it", container.container_id)
                    if self._restart_container(context, capsule, container):
                        # Counters cleared so the new container is judged on
                        # its own results; the restart tally is kept by the
                        # restart itself.
                        state = {'restarts': _restart_count(container)}
                self._save_probe_state(context, container, state)
                return True

        readiness = probes.get('readinessProbe')
        if readiness and self._probe_due(state, 'readiness', readiness,
                                         container, now):
            self._schedule_next(state, 'readiness', readiness, now)
            ready = self._probe_passed(container, readiness, state, 'readiness')
            # Recorded on every pass, not only on a change: the reader treats a
            # missing value as "never answered" and keeps traffic away, so a
            # probe that passes first time and never changes would leave the
            # container permanently unready.
            if state.get('ready') != ready:
                LOG.info("Container %(id)s readiness changed to %(ready)s",
                         {'id': container.container_id, 'ready': ready})
                changed = True
            state['ready'] = ready

        liveness = probes.get('livenessProbe')
        if liveness and self._probe_due(state, 'liveness', liveness,
                                        container, now):
            self._schedule_next(state, 'liveness', liveness, now)
            if self._probe_passed(container, liveness, state, 'liveness'):
                pass
            elif self._probe_failed_enough(liveness, state, 'liveness'):
                LOG.info("Liveness probe for container %s failed; restarting "
                         "it", container.container_id)
                if self._restart_container(context, capsule, container):
                    # Counters cleared so the new container is judged on its
                    # own results; the restart tally is what tells anyone above
                    # that this keeps happening.
                    state = {'restarts': _restart_count(container)}
                changed = True

        self._save_probe_state(context, container, state)
        return changed

    @staticmethod
    def _probe_due(state, kind, probe, container, now):
        """Whether this probe should run yet.

        Before its first run the container's own start time plus
        initialDelaySeconds decides, which is what stops a slow-starting
        application from being killed for failing a check it was never given
        time to satisfy.
        """
        due = state.get(kind + '_next')
        if due is not None:
            return now >= due
        delay = int(probe.get('initialDelaySeconds') or 0)
        # ⚠️ created_at is the fallback, and it has to be one. started_at is
        # not always set, and `return delay == 0` for that case was a deadlock:
        # a probe with initialDelaySeconds above zero was never due, so it
        # never ran, so _schedule_next never wrote the _next it would have
        # been judged by afterwards, so it was never due again. The container
        # stayed unready for its whole life with nothing logged, because a
        # probe that does not run cannot fail.
        #
        # It hit exactly the pods we ask tenants to write: docs/tenant-guide
        # tells them to set initialDelaySeconds because a Kata guest is slow
        # to start.
        started = (getattr(container, 'started_at', None) or
                   getattr(container, 'created_at', None))
        if started is None:
            return True
        return now >= started.timestamp() + delay

    @staticmethod
    def _schedule_next(state, kind, probe, now):
        period = int(probe.get('periodSeconds') or 10)
        state[kind + '_next'] = now + max(period, 1)

    def _probe_passed(self, container, probe, state, kind):
        """Run one probe and track consecutive results in state."""
        ok = self._run_probe(container, probe)
        fail_key, ok_key = kind + '_failures', kind + '_successes'
        if ok:
            state[fail_key] = 0
            state[ok_key] = state.get(ok_key, 0) + 1
            threshold = int(probe.get('successThreshold') or 1)
            return state[ok_key] >= threshold
        state[ok_key] = 0
        state[fail_key] = state.get(fail_key, 0) + 1
        return False

    def _probe_failed_enough(self, probe, state, kind):
        """Whether a probe has failed the number of times it is allowed to.

        Acting on a single failure would restart a container for one dropped
        connection, which is why Kubernetes has failureThreshold at all.
        """
        threshold = int(probe.get('failureThreshold') or 3)
        return state.get(kind + '_failures', 0) >= threshold

    def _save_probe_state(self, context, container, state):
        healthcheck = dict(container.healthcheck or {})
        if healthcheck.get('k8s_probe_state') == state:
            return
        healthcheck['k8s_probe_state'] = state
        container.healthcheck = healthcheck
        container.save(context)

    def _restart_container(self, context, capsule, container):
        """Replace a container in place, keeping its sandbox.

        A CRI container runs once: after it stops, the runtime refuses to start
        it again ("container is in CONTAINER_EXITED state"), so a restart means
        creating a new one. That was the whole gap behind a liveness probe --
        it fired, the start was rejected, and the pod went on reporting healthy
        with a dead application inside it.

        The sandbox is reused rather than recreated, so the capsule keeps its
        address. Losing it would send every client of this pod to look the
        service up again, which a container restart has no business causing.

        Returns True when the container was replaced.
        """
        old_id = container.container_id
        # Recorded before the replacement is created: the runtime is told the
        # attempt number, and it reads it back off the container.
        restarts = _restart_count(container) + 1
        healthcheck = dict(container.healthcheck or {})
        state = dict(healthcheck.get('k8s_probe_state') or {})
        state['restarts'] = restarts
        healthcheck['k8s_probe_state'] = state
        container.healthcheck = healthcheck

        try:
            try:
                self.runtime_stub.StopContainer(
                    api_pb2.StopContainerRequest(
                        container_id=old_id, timeout=10))
            except grpc.RpcError as e:
                # Already gone or already stopped, which is where a failing
                # liveness probe usually finds it.
                LOG.debug("Container %(id)s did not need stopping: %(err)s",
                          {'id': old_id, 'err': e})

            volumes = {container.uuid: objects.VolumeMapping.list_by_container(
                context, container.uuid)}
            self._create_container(context, capsule, container, volumes)

            try:
                self.runtime_stub.RemoveContainer(
                    api_pb2.RemoveContainerRequest(container_id=old_id))
            except grpc.RpcError as e:
                # The replacement is already running; a leftover record costs
                # disk, not correctness.
                LOG.warning("Could not remove replaced container %(id)s: "
                            "%(err)s", {'id': old_id, 'err': e})
            return True
        except Exception as e:
            # Left visible rather than swallowed: a restart that silently fails
            # is the failure the probe existed to catch, now hidden one level
            # deeper.
            LOG.error("Could not restart container %(id)s: %(err)s",
                      {'id': old_id, 'err': e})
            return False

    # ------------------------------------------------------------------
    # ContainerDriver: a container is a sandbox with one container in it.
    #
    # Every helper below this line is the one the capsule path already uses.
    # The container plays both roles -- it owns the sandbox and it is the
    # container -- which works because Capsule and Container are sibling
    # classes over one table and carry the same fields.
    # ------------------------------------------------------------------

    # The label that ties a sandbox back to the container that owns it. The
    # container records the container id, because that is what exec, logs and
    # stats need; the sandbox is found by this label, so a create interrupted
    # between the two leaves something a sweep can still recognise rather than
    # an anonymous sandbox nobody will ever claim.
    OWNER_LABEL = 'io.zun.container.uuid'

    # The runtime pulls each container's image as it is created,
    # for capsules and plain containers alike.
    pulls_own_images = True

    def create(self, context, container, image, requested_networks,
               requested_volumes):
        """Create a container in a sandbox of its own, without starting it.

        Created, not started: the Container API separates the two, and the
        caller starts it afterwards when it was asked to run. The capsule path
        does start what it creates -- a capsule has no half-built state to be
        in -- so this cannot simply reuse it and must undo that one step.
        """
        self._create_pod_sandbox(context, container, requested_networks,
                                 labels={self.OWNER_LABEL: container.uuid})
        # The sandbox id lands in container_id; _create_container reads it as
        # the sandbox to place into and then overwrites it with the container
        # id it gets back. Both are needed for a moment and only one field
        # holds them, which is why the sandbox is labelled.
        self._create_container(context, container, container,
                               requested_volumes, start=False)
        container.status = consts.CREATED
        container.status_reason = None
        return container

    def check_container_exist(self, container):
        """Whether the runtime still has this container.

        Asked before a rebuild, to decide whether there is anything to tear
        down. ⚠️ Answering by exception -- which is what not implementing it
        did -- makes rebuild fail without changing anything, and a rebuild that
        changes nothing looks exactly like one that worked.
        """
        if not container.container_id:
            return False
        try:
            response = self.runtime_stub.ListContainers(
                api_pb2.ListContainersRequest(
                    filter=api_pb2.ContainerFilter(
                        id=container.container_id)))
        except grpc.RpcError as e:
            LOG.debug('Could not look for container %(id)s: %(err)s',
                      {'id': container.container_id, 'err': e})
            return False
        return bool(response.containers)

    def _sandbox_of(self, container):
        """Find the sandbox holding this container.

        By label rather than by a stored id: there is one id field and the
        container id has the stronger claim on it. Falls back to asking the
        runtime which sandbox the container is in, which covers a container
        created before the label existed.
        """
        try:
            response = self.runtime_stub.ListPodSandbox(
                api_pb2.ListPodSandboxRequest(
                    filter=api_pb2.PodSandboxFilter(
                        label_selector={self.OWNER_LABEL: container.uuid})))
            if response.items:
                return response.items[0].id
        except grpc.RpcError as e:
            LOG.debug('Could not list sandboxes by owner label: %s', e)

        if not container.container_id:
            return None
        try:
            response = self.runtime_stub.ListContainers(
                api_pb2.ListContainersRequest(
                    filter=api_pb2.ContainerFilter(id=container.container_id)))
            if response.containers:
                return response.containers[0].pod_sandbox_id
        except grpc.RpcError as e:
            LOG.debug('Could not find the sandbox of %s: %s',
                      container.container_id, e)
        return None

    def delete(self, context, container, force):
        """Remove a container and the sandbox that held it.

        The sandbox goes too: it exists only for this container, and leaving
        it keeps a network namespace, a Neutron port and, under Kata, a virtual
        machine.
        """
        pod_id = self._sandbox_of(container)
        if pod_id:
            self._delete_sandbox(context, container, pod_id)
        elif container.container_id:
            # No sandbox to be found, but the container is recorded: remove
            # what can be removed rather than leaving both.
            self._remove_container(container.container_id)
        self._delete_neutron_ports(context, container)

    def start(self, context, container):
        if not container.container_id:
            raise exception.ZunException(_(
                'Container %s was never created on this host') % container.uuid)
        try:
            self.runtime_stub.StartContainer(
                api_pb2.StartContainerRequest(
                    container_id=container.container_id))
        except grpc.RpcError as e:
            if 'CONTAINER_EXITED' not in (e.details() or ''):
                raise
            # A CRI container is not a docker container. Once it has exited the
            # runtime will not start it again and offers no flag that makes it
            # -- but docker will, and the Container API was written against
            # docker, so "stop then start" is a thing users expect to work.
            # Rebuilding it in the sandbox it died in is what keeps that
            # promise, and keeps the address: the address belongs to the
            # sandbox, not to the container.
            self._restart_exited(context, container)
        container.status = consts.RUNNING
        container.status_reason = None
        return container

    def _restart_exited(self, context, container):
        """Give an exited container a fresh incarnation in its own sandbox."""
        sandbox = self._sandbox_of(container)
        if sandbox is None:
            raise exception.ZunException(_(
                'The sandbox that held container %s is gone, so it cannot be '
                'started again') % container.uuid)
        # ⚠️ Build the replacement before removing the corpse. The record holds
        # one container id; show() marks a container Error when the runtime
        # does not know that id, so any instant where it names nothing is an
        # instant where a concurrent status read fails the container for good.
        # Removing first left a window measured in seconds -- long enough that
        # a caller polling every three seconds hit it and a test sleeping
        # fourteen never did. Creating first keeps the recorded id naming
        # something that exists at every instant: the old one until the new one
        # is saved.
        dead = container.container_id
        volmaps = objects.VolumeMapping.list_by_container(context,
                                                          container.uuid)
        # Both incarnations are in the sandbox at once, so the new one needs a
        # name of its own: the runtime names a container by its name and its
        # attempt number, and reusing both would collide with what is still
        # there.
        attempt = self._attempt_of(dead) + 1
        # _create_container reads the sandbox out of this field and then
        # overwrites it with the id of what it made.
        container.container_id = sandbox
        try:
            self._create_container(context, container, container,
                                   {container.uuid: volmaps} if volmaps else {},
                                   start=True, attempt=attempt)
        except Exception:
            with excutils.save_and_reraise_exception():
                # The record still points at the dead container, which is a
                # truer thing to say than the sandbox id it was holding.
                container.container_id = dead
        self._remove_container(dead)

    def _attempt_of(self, container_id):
        """Which incarnation the runtime thinks this container is."""
        try:
            response = self.runtime_stub.ListContainers(
                api_pb2.ListContainersRequest(
                    filter=api_pb2.ContainerFilter(id=container_id)))
            if response.containers:
                return response.containers[0].metadata.attempt
        except grpc.RpcError as e:
            LOG.debug('Could not read the attempt number of %(id)s: %(err)s',
                      {'id': container_id, 'err': e})
        return 0

    def reboot(self, context, container, timeout=None):
        """Stop it, then start it again.

        The runtime has no reboot of its own, and on this driver the second
        half is not a plain start: an exited CRI container cannot be started,
        so it is rebuilt inside its sandbox. That is also why the address
        survives a reboot -- it was never the container's.
        """
        self.stop(context, container, timeout)
        return self.start(context, container)

    # containerd looks in this namespace for anything the CRI created. Without
    # the header it looks in the default one, finds nothing, and reports a
    # running container as not found.
    _CTRD_NS = (('containerd-namespace', 'k8s.io'),)

    def pause(self, context, container):
        """Freeze the container.

        ⚠️ Frozen, not released. This is the freezer cgroup, applied inside the
        guest by the kata agent, so from the host nothing changes at all: the
        VMM still holds the whole of the guest's memory and the placement claim
        does not move. CPU time stops being spent; nothing is given back. A
        product calling this "pause" should not let anyone read it as "stops
        costing money".
        """
        self.task_stub.Pause(
            tasks_pb2.PauseTaskRequest(container_id=container.container_id),
            metadata=self._CTRD_NS)
        container.status = consts.PAUSED
        container.status_reason = None
        return container

    def unpause(self, context, container):
        self.task_stub.Resume(
            tasks_pb2.ResumeTaskRequest(container_id=container.container_id),
            metadata=self._CTRD_NS)
        container.status = consts.RUNNING
        container.status_reason = None
        return container

    def kill(self, context, container, signal=None):
        """Send a signal to the container's process.

        Through containerd's task service rather than the CRI, which has no
        call for this: StopContainer would answer a SIGHUP asking a server to
        reload its configuration by killing the workload.
        """
        number = _signal_number(signal)
        self.task_stub.Kill(
            tasks_pb2.KillRequest(container_id=container.container_id,
                                  signal=number, all=False),
            metadata=self._CTRD_NS)
        return container

    def resize(self, context, container, height, weight):
        """Not available here, and not a gap that can be filled.

        A tty's size travels inside the stream -- the fifth channel of the
        protocol the runtime's streaming server speaks -- and an interactive
        session resizes that way today. What has no equivalent is resizing
        from outside the stream: this is a separate request that cannot reach
        an already-open one, and the runtime offers no call for it.
        """
        raise exception.Invalid(_(
            'This runtime carries the terminal size inside the session, so it '
            'cannot be set from outside one'))

    def network_attach(self, context, container, requested_network):
        raise exception.Invalid(_(
            'A sandbox is given its networks when it is created and this '
            'runtime offers no way to change them afterwards'))

    def network_detach(self, context, container, network):
        raise exception.Invalid(_(
            'A sandbox is given its networks when it is created and this '
            'runtime offers no way to change them afterwards'))

    def _task_status(self, container_id):
        """What containerd says the task is doing, or None if it cannot say."""
        try:
            response = self.task_stub.Get(
                tasks_pb2.GetRequest(container_id=container_id),
                metadata=self._CTRD_NS)
        except grpc.RpcError as e:
            LOG.debug('Could not read task state of %(id)s: %(err)s',
                      {'id': container_id, 'err': e})
            return None
        return response.process.status

    def update(self, context, container):
        """Apply changed cpu and memory limits to a running container."""
        patch = container.obj_get_changes()
        if patch.get('cpu') is None and patch.get('memory') is None:
            return container
        # Fields not in the patch stay as the record holds them: a resource
        # update is the whole linux block, and sending memory alone would
        # reset cpu to zero -- unlimited -- on the way past.
        resources = cri_resources.linux_resources(
            patch.get('cpu', container.cpu),
            patch.get('memory', container.memory))
        self.runtime_stub.UpdateContainerResources(
            api_pb2.UpdateContainerResourcesRequest(
                container_id=container.container_id,
                linux=api_pb2.LinuxContainerResources(**resources)))
        return container

    def stats(self, context, container):
        """What the container is using, in the shape the API documents.

        ⚠️ The runtime reports CPU as a counter of nanoseconds burned, not a
        rate, so a percentage cannot be read from one sample. Two samples a
        second apart give the rate honestly; a single sample divided by
        anything would be a number that looks like a percentage and is not.
        """
        first = self._container_stats(container.container_id)
        time.sleep(1)
        second = self._container_stats(container.container_id)

        cpu_percent = 0.0
        elapsed = second['timestamp'] - first['timestamp']
        if elapsed > 0:
            cores = os.cpu_count() or 1
            burned = second['cpu_ns'] - first['cpu_ns']
            cpu_percent = float(burned) / float(elapsed) / cores * 100

        mem_usage = second['memory'] / 1024 / 1024
        mem_limit = 0
        if container.memory:
            mem_limit = float(container.memory)
        mem_percent = (mem_usage / mem_limit * 100) if mem_limit else 0.0

        return {"CONTAINER": container.name,
                "CPU %": cpu_percent,
                "MEM USAGE(MiB)": mem_usage,
                "MEM LIMIT(MiB)": mem_limit,
                "MEM %": mem_percent,
                # The runtime accounts for the writable layer, not for block
                # reads and writes. Reporting its size here would put a real
                # number under a heading that means something else.
                # ⚠️ Genuinely unavailable, not merely unfetched: the CRI's
                # IoUsage carries pressure-stall figures and no byte counts,
                # and a kata container has no cgroup of its own on the host to
                # read them from -- containerd's own task metrics return only
                # pids, cpu and memory for one. Reporting the writable
                # layer's size here would put a real number under a heading
                # that means something else.
                #
                # "-- / --" rather than a zero, and spelled as docker's own
                # client spells an unavailable figure (cli/command/container
                # /formatter_stats.go): zero reads as an idle disk, which is
                # a claim nobody measured.
                "BLOCK I/O(B)": self._NO_VALUE_PAIR,
                "NET I/O(B)": self._net_io(container)}

    # What docker's client prints for a figure it has no value for.
    _NO_VALUE_PAIR = '-- / --'

    def _net_io(self, container):
        """Bytes in and out of the capsule's network namespace.

        The counters belong to the sandbox rather than to a container: every
        container in a capsule shares one namespace, so there is one figure
        and it is the capsule's. ContainerStats has no network field at all --
        asking it was why this read "unavailable" for so long.

        Unreadable answers "--" for the same reason a missing block figure
        does: a zero here would be a claim that nothing crossed the link.
        """
        # The sandbox, resolved the way everything else here resolves it --
        # by owner label, falling back to the container's own listing. A
        # container id is not a sandbox id, and passing one where the other
        # belongs asks the runtime about something that does not exist.
        pod_id = self._sandbox_of(container)
        if not pod_id:
            return self._NO_VALUE_PAIR
        try:
            resp = self.runtime_stub.PodSandboxStats(
                api_pb2.PodSandboxStatsRequest(pod_sandbox_id=pod_id))
        except grpc.RpcError as e:
            LOG.debug('No network figures for %(id)s: %(err)s',
                      {'id': pod_id, 'err': e})
            return self._NO_VALUE_PAIR
        net = resp.stats.linux.network
        iface = net.default_interface
        if not net.HasField('default_interface'):
            return self._NO_VALUE_PAIR
        return '%s / %s' % (iface.rx_bytes.value, iface.tx_bytes.value)

    def measure_writable_layers(self, context, containers):
        wanted = {c.container_id: c.uuid for c in containers
                  if c.container_id}
        if not wanted:
            return {}
        response = self.runtime_stub.ListContainerStats(
            api_pb2.ListContainerStatsRequest())
        found = {}
        for st in response.stats:
            uuid = wanted.get(st.attributes.id)
            if uuid is None:
                continue
            # The runtime accounts for the writable layer as a filesystem
            # of its own, which is exactly the figure wanted here.
            found[uuid] = int(st.writable_layer.used_bytes.value)
        return found

    def _container_stats(self, container_id):
        response = self.runtime_stub.ContainerStats(
            api_pb2.ContainerStatsRequest(container_id=container_id))
        st = response.stats
        return {'timestamp': st.cpu.timestamp,
                'cpu_ns': st.cpu.usage_core_nano_seconds.value,
                'memory': st.memory.working_set_bytes.value}

    def top(self, context, container, ps_args=None):
        """Processes, as the container itself sees them.

        There is no CRI call for this: the only vantage point on a kata
        sandbox is inside it, so this runs ps in the container and needs the
        image to carry one. An image without ps says so plainly rather than
        returning an empty list of processes.

        The api-ref fixes the answer as {"Titles": [...], "Processes":
        [[...]]} and it is the same answer whichever driver served it --
        a caller reads one API, not one driver. Returning ps output raw
        left every consumer to guess the shape from what it received.
        """
        argv = ['ps'] + shlex.split(ps_args) if ps_args else ['ps', '-ef']
        exit_code, out, err = self._exec_in_container(
            container.container_id, argv, CONF.cri_exec_timeout)
        if exit_code != 0:
            raise exception.Invalid(_(
                'Could not list processes in the container: %s')
                % ((err or out or '').strip()[:200] or 'ps is not in the image'))
        return driver.process_table(out)

    def stop(self, context, container, timeout=None):
        if not container.container_id:
            return container
        self.runtime_stub.StopContainer(api_pb2.StopContainerRequest(
            container_id=container.container_id,
            timeout=int(timeout or CONF.docker.default_timeout)))
        container.status = consts.STOPPED
        container.status_reason = None
        return container

    def show(self, context, container):
        """Refresh a container's state from the runtime."""
        if not container.container_id:
            return container
        try:
            return self._show_container(context, container)
        except exception.ZunException:
            if container.task_state:
                # ⚠️ An operation owns this container and is somewhere between
                # tearing down one incarnation and building the next, so the
                # runtime not knowing the id is expected rather than news. The
                # operation reports its own outcome; a status read that lands
                # in that gap must not pre-empt it. Marking it Error here made
                # a rebuild fail from the outside while it was still working,
                # and left the container Error after it had succeeded.
                return container
            # Gone from the runtime with nothing operating on it. Saying so
            # beats reporting the last state it was seen in, which reads as
            # running.
            container.status = consts.ERROR
            container.status_reason = _('Container is not found in runtime')
            return container

    def list(self, context):
        """Every container this driver manages on this host.

        Returned as (containers, sandboxes-with-no-record). The second is what
        the caller needs to notice a sandbox whose container row is gone --
        the shape a create interrupted halfway leaves behind.
        """
        db_containers = objects.Container.list_by_host(context, CONF.host)
        by_id = {c.container_id: c for c in db_containers if c.container_id}

        try:
            response = self.runtime_stub.ListContainers(
                api_pb2.ListContainersRequest())
        except grpc.RpcError as e:
            LOG.warning('Could not list containers: %s', e)
            return db_containers, []

        unrecorded = []
        for item in response.containers:
            recorded = by_id.get(item.id)
            if recorded is not None:
                self._populate_container_state(recorded, item)
            elif item.labels.get(self.OWNER_LABEL):
                unrecorded.append(item.id)
        return db_containers, unrecorded

    def get_websocket_url(self, context, container):
        """Where to attach to this container's main process.

        The runtime serves the stream itself, on this node's loopback -- which
        is why zun-wsproxy runs here. Attach rather than exec: this is the
        process the container was created to run, not a new one.
        """
        if not container.container_id:
            raise exception.ZunException(_(
                'Container %s was never created on this host') % container.uuid)
        response = self.runtime_stub.Attach(api_pb2.AttachRequest(
            container_id=container.container_id,
            tty=bool(container.tty or container.interactive),
            stdin=True,
            stdout=True,
            # A terminal merges the two, and asking for both is refused.
            stderr=not (container.tty or container.interactive),
        ))
        if not response.url:
            raise exception.ZunException(_(
                'the runtime returned no streaming url for attach'))
        return response.url

    def get_logs_url(self, context, container, stdout=True, stderr=True):
        """Where to follow this container's output.

        The same Attach the session above opens, asked for without stdin.
        The runtime's own log file is what show_logs reads, and following a
        file it appends to would mean watching it from here -- the stream it
        already serves is the thing being followed, and it is served on the
        node zun-wsproxy runs on for the same reason attach is.
        """
        if not container.container_id:
            raise exception.ZunException(
                _('Container %s was never created on this host')
                % container.uuid)
        tty = bool(container.tty or container.interactive)
        response = self.runtime_stub.Attach(api_pb2.AttachRequest(
            container_id=container.container_id,
            tty=tty,
            stdin=False,
            stdout=bool(stdout),
            # A terminal merges the two, and asking for both is refused.
            stderr=bool(stderr) and not tty,
        ))
        if not response.url:
            raise exception.ZunException(_(
                'the runtime returned no streaming url to follow logs'))
        return response.url

    def pull_image(self, context, repo, tag, image_pull_policy='always',
                   driver_name=None, registry=None):
        """Fetch an image through the runtime, which is where it has to end up.

        Not delegated to zun's image drivers, which is what the docker driver
        does: the default one of those talks to a docker daemon, and there is
        none here -- an attempt reaches a socket that does not exist and fails
        as ENOENT, several layers from anything that names an image.

        The runtime's own image service is also what the capsule path has
        always used, so both shapes now pull the same way into the same store,
        which was the point of putting them on one runtime.

        image_loaded is True because there is nothing left to load: the image
        is in the runtime's store, not in a file waiting to be imported.
        """
        ref = '%s:%s' % (repo, tag) if tag else repo
        try:
            self._pull_image_ref(ref)
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                raise exception.ImageNotFound(image=ref)
            raise exception.ZunException(
                _('could not pull %(ref)s: %(err)s')
                % {'ref': ref, 'err': e})
        return {'image': ref, 'path': None, 'driver': 'cri'}, True

    def search_image(self, context, repo, tag, driver_name, exact_match):
        # A container runtime has no registry search. Saying so beats
        # answering with an empty list, which reads as "no such image".
        raise exception.OperationNotSupported(message=_(
            'this runtime cannot search a registry; name the image exactly'))

    def capsule_stats(self, context, capsule):
        """Return per-container resource usage for one capsule.

        The runtime already accounts for this -- it is what a kubelet reads to
        answer "kubectl top" and what autoscaling on CPU or memory is driven
        from. Without it a capsule is a black box: its owner can see that the
        workload runs and nothing about what it uses, and a
        HorizontalPodAutoscaler has no signal at all.

        CPU arrives as a cumulative count of nanoseconds rather than a rate, so
        it is passed on exactly as the runtime gives it, with its timestamp.
        Turning two readings into a rate belongs to the caller: only the caller
        knows which earlier reading belongs to the same container, and a rate
        computed here would be wrong across a restart.
        """
        pod_id = capsule.container_id
        if not pod_id:
            return {'stats': [], 'network': None}

        response = self.runtime_stub.ListContainerStats(
            api_pb2.ListContainerStatsRequest(
                filter=api_pb2.ContainerStatsFilter(pod_sandbox_id=pod_id)))

        out = []
        for stats in response.stats:
            entry = {
                'container_id': stats.attributes.id,
                'name': stats.attributes.metadata.name,
            }
            if stats.HasField('cpu'):
                entry['timestamp'] = stats.cpu.timestamp
                if stats.cpu.HasField('usage_core_nano_seconds'):
                    entry['cpu_usage_core_nanoseconds'] = \
                        stats.cpu.usage_core_nano_seconds.value
            if stats.HasField('memory'):
                entry.setdefault('timestamp', stats.memory.timestamp)
                if stats.memory.HasField('working_set_bytes'):
                    entry['memory_working_set_bytes'] = \
                        stats.memory.working_set_bytes.value
                if stats.memory.HasField('usage_bytes'):
                    entry['memory_usage_bytes'] = \
                        stats.memory.usage_bytes.value
            out.append(entry)
        return {'stats': out, 'network': self._sandbox_network(pod_id)}

    def _sandbox_network(self, pod_id):
        """Bytes across the capsule's own network namespace.

        One figure for the capsule rather than one per container: the
        containers share a namespace, so there is one link and it belongs to
        the sandbox. ⚠️ Only PodSandboxStats carries it -- the container-level
        messages have no network field at all, which is why this read as
        unavailable for as long as it was fetched from there.

        None rather than zeros when it cannot be read: a zero is a claim that
        nothing crossed the link, and the caller has no way to tell that claim
        from silence.
        """
        try:
            resp = self.runtime_stub.PodSandboxStats(
                api_pb2.PodSandboxStatsRequest(pod_sandbox_id=pod_id))
        except grpc.RpcError as e:
            LOG.debug('No network figures for sandbox %(id)s: %(err)s',
                      {'id': pod_id, 'err': e})
            return None
        net = resp.stats.linux.network
        if not net.HasField('default_interface'):
            return None
        iface = net.default_interface
        return {
            'timestamp': net.timestamp,
            'name': iface.name,
            'rx_bytes': iface.rx_bytes.value,
            'rx_errors': iface.rx_errors.value,
            'tx_bytes': iface.tx_bytes.value,
            'tx_errors': iface.tx_errors.value,
        }

    def delete_capsule(self, context, capsule, force):
        pod_id = capsule.container_id
        if not pod_id:
            return
        self._delete_sandbox(context, capsule, pod_id)
        self._delete_neutron_ports(context, capsule)

    def _delete_sandbox(self, context, owner, pod_id):
        """Stop and remove a sandbox, whether it held one container or many.

        A sandbox that is already gone is not an error: this runs on the delete
        path, and the caller's goal is that it not be there.

        ⚠️ Stopping and removing are attempted separately, because removal is
        the goal and stopping is only the courteous way to reach it. Held in
        one try, a stop that hangs takes the removal with it -- and a stop
        hangs whenever the shim is gone, since the runtime waits for a task
        nothing will answer for. The capsule then cannot be deleted, at all,
        ever: every retry waits for the same absent shim, the record stays,
        and the resources stay accounted for with nothing visibly holding
        them. Measured on 42 capsules that had been undeletable for five days.

        Removing a sandbox that could not be stopped is what the CRI asks for
        anyway -- RemovePodSandbox is specified to force-terminate whatever is
        still running inside.
        """
        try:
            response = self.runtime_stub.StopPodSandbox(
                api_pb2.StopPodSandboxRequest(pod_sandbox_id=pod_id))
            LOG.debug("podsandbox is stopped: %s", response)
        except exception.CommandError as e:
            if 'error occurred when try to find sandbox' in str(e):
                LOG.debug("cannot find pod sandbox %s in runtime", pod_id)
            else:
                raise
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                LOG.debug("podsandbox %s was already gone", pod_id)
            elif e.code() in (grpc.StatusCode.DEADLINE_EXCEEDED,
                              grpc.StatusCode.UNAVAILABLE):
                LOG.warning("Could not stop podsandbox %(id)s (%(code)s); "
                            "removing it anyway, since waiting again would "
                            "wait for the same thing: %(err)s",
                            {'id': pod_id, 'code': e.code(), 'err': e})
            else:
                raise

        try:
            response = self.runtime_stub.RemovePodSandbox(
                api_pb2.RemovePodSandboxRequest(pod_sandbox_id=pod_id))
            LOG.debug("podsandbox is removed: %s", response)
        except exception.CommandError as e:
            if 'error occurred when try to find sandbox' in str(e):
                LOG.debug("pod sandbox %s was already gone", pod_id)
            else:
                raise
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise
            LOG.debug("podsandbox %s was already gone", pod_id)

    def _remove_container(self, container_id):
        """Remove one container, quietly when it is already gone."""
        try:
            self.runtime_stub.RemoveContainer(
                api_pb2.RemoveContainerRequest(container_id=container_id))
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise

    def _delete_neutron_ports(self, context, capsule):
        if not capsule.addresses:
            return

        neutron_ports = set()
        all_ports = set()
        for net_uuid, addrs_list in capsule.addresses.items():
            for addr in addrs_list:
                all_ports.add(addr['port'])
                if not addr['preserve_on_delete']:
                    port_id = addr['port']
                    neutron_ports.add(port_id)

        neutron_api = neutron.NeutronAPI(context)
        neutron_api.delete_or_unbind_ports(all_ports, neutron_ports)
