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
from oslo_log import log as logging
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
from zun.criapi import api_pb2
from zun.criapi import api_pb2_grpc
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


def _annotation_list(capsule, key):
    raw = (capsule.annotations or {}).get(key)
    if not raw:
        return []
    return [s for s in (part.strip() for part in raw.split(',')) if s]


def _dns_searches(capsule):
    return _annotation_list(capsule, DNS_SEARCHES_ANNOTATION)


def _dns_servers(capsule):
    return _annotation_list(capsule, DNS_SERVERS_ANNOTATION)


def _restart_count(container):
    """How many times a probe has had this container replaced."""
    state = (container.healthcheck or {}).get('k8s_probe_state') or {}
    try:
        return int(state.get('restarts') or 0)
    except (TypeError, ValueError):
        return 0


class CriDriver(driver.BaseDriver, driver.CapsuleDriver):
    """Implementation of container drivers for CRI runtime."""

    # TODO(hongbin): define a list of capabilities of this driver.
    capabilities = {}

    def __init__(self):
        super(CriDriver, self).__init__()
        channel = grpc.insecure_channel(
            'unix:///run/containerd/containerd.sock')
        self.runtime_stub = api_pb2_grpc.RuntimeServiceStub(channel)
        self.image_stub = api_pb2_grpc.ImageServiceStub(channel)

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

    def _create_pod_sandbox(self, context, capsule, requested_networks):
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
            capsule, servers, _dns_searches(capsule))
        sandbox_resp = self.runtime_stub.RunPodSandbox(
            api_pb2.RunPodSandboxRequest(
                config=sandbox_config,
                runtime_handler=runtime,
            )
        )
        LOG.debug("podsandbox is created: %s", sandbox_resp)
        capsule.container_id = sandbox_resp.pod_sandbox_id

    def _get_sandbox_config(self, capsule, dns_servers=None,
                            dns_searches=None):
        config = api_pb2.PodSandboxConfig(
            metadata=api_pb2.PodSandboxMetadata(
                name=capsule.uuid, namespace="default", uid=capsule.uuid
            ),
            # Without a log directory the runtime discards a container's output
            # entirely: there is no stream to attach to after the fact and
            # nothing on disk, so the logs API has nothing to serve.
            log_directory=self._log_directory(),
        )
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
                          requested_volumes):
        # pull image
        self._pull_image(context, container)

        sandbox_config = self._get_sandbox_config(capsule)
        container_config = self._get_container_config(context, capsule, container,
                                                      requested_volumes)
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

        response = self.runtime_stub.StartContainer(
            api_pb2.StartContainerRequest(
                container_id=container.container_id
            )
        )
        LOG.debug("container is started: %s", response)

    def _get_container_config(self, context, capsule, container, requested_volumes):
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

        cpu = 0
        if container.cpu is not None:
            cpu = int(1024 * container.cpu)
        memory = 0
        if container.memory is not None:
            memory = int(container.memory) * 1024 * 1024
        linux_config = api_pb2.LinuxContainerConfig(
            security_context=_linux_security_context(container),
            resources={
                'cpu_shares': cpu,
                'memory_limit_in_bytes': memory,
            }
        )

        # The attempt number is what distinguishes one incarnation of a
        # container from the next, both in the runtime's own naming and in what
        # crictl shows, so a restarted container does not look like the
        # original still running.
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
        # TODO(hongbin): add support for private registry
        response = self.image_stub.PullImage(
            api_pb2.PullImageRequest(
                image=api_pb2.ImageSpec(image=container.image)
            )
        )
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
            container.status = consts.RUNNING
        elif state == api_pb2.ContainerState.CONTAINER_EXITED:
            container.status = consts.STOPPED
        elif state == api_pb2.ContainerState.CONTAINER_UNKNOWN:
            LOG.debug('State is unknown, status: %s', state)
            container.status = consts.UNKNOWN
        else:
            LOG.warning('Receive unexpected state from CRI runtime: %s', state)
            container.status = consts.UNKNOWN
            container.status_reason = "container state unknown"

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
                cmd=cmd,
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
            # An interactive session needs the runtime's streaming Exec and a
            # URL to attach to, which is a different endpoint from the one used
            # here. Refused rather than run non-interactively, which would hang
            # a caller waiting to type.
            raise exception.Invalid(
                _('Interactive exec is not supported on a capsule'))
        return container.container_id

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

            changed = False
            running = 0
            counted = 0
            for container in capsule.containers:
                old_status = container.status
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

            if not counted:
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
            return []

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
        return out

    def delete_capsule(self, context, capsule, force):
        pod_id = capsule.container_id
        if not pod_id:
            return

        try:
            response = self.runtime_stub.StopPodSandbox(
                api_pb2.StopPodSandboxRequest(
                    pod_sandbox_id=capsule.container_id,
                )
            )
            LOG.debug("podsandbox is stopped: %s", response)
            response = self.runtime_stub.RemovePodSandbox(
                api_pb2.RemovePodSandboxRequest(
                    pod_sandbox_id=capsule.container_id,
                )
            )
            LOG.debug("podsandbox is removed: %s", response)
        except exception.CommandError as e:
            if 'error occurred when try to find sandbox' in str(e):
                LOG.error("cannot find pod sandbox in runtime")
                pass
            else:
                raise

        self._delete_neutron_ports(context, capsule)

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
