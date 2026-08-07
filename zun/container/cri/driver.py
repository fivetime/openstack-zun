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

import grpc
from oslo_log import log as logging
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
                                   requested_networks,
                                   requested_volumes)
            self._wait_for_init_container(context, container)
            container.save(context)

        for container in capsule.containers:
            self._create_container(context, capsule, container,
                                   requested_networks,
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
        sandbox_config = self._get_sandbox_config(capsule, dns_servers)
        sandbox_resp = self.runtime_stub.RunPodSandbox(
            api_pb2.RunPodSandboxRequest(
                config=sandbox_config,
                runtime_handler=runtime,
            )
        )
        LOG.debug("podsandbox is created: %s", sandbox_resp)
        capsule.container_id = sandbox_resp.pod_sandbox_id

    def _get_sandbox_config(self, capsule, dns_servers=None):
        config = api_pb2.PodSandboxConfig(
            metadata=api_pb2.PodSandboxMetadata(
                name=capsule.uuid, namespace="default", uid=capsule.uuid
            )
        )
        if dns_servers:
            config.dns_config.servers.extend(dns_servers)
        return config

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
                          requested_networks, requested_volumes):
        # pull image
        self._pull_image(context, container)

        sandbox_config = self._get_sandbox_config(capsule)
        container_config = self._get_container_config(context, container,
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

    def _get_container_config(self, context, container, requested_volumes):
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
        working_dir = container.workdir or ""
        labels = container.labels or []

        cpu = 0
        if container.cpu is not None:
            cpu = int(1024 * container.cpu)
        memory = 0
        if container.memory is not None:
            memory = int(container.memory) * 1024 * 1024
        linux_config = api_pb2.LinuxContainerConfig(
            security_context=api_pb2.LinuxContainerSecurityContext(
                privileged=container.privileged
            ),
            resources={
                'cpu_shares': cpu,
                'memory_limit_in_bytes': memory,
            }
        )

        # TODO(hongbin): add support for entrypoint
        return api_pb2.ContainerConfig(
            metadata=api_pb2.ContainerMetadata(name=container.name),
            image=api_pb2.ImageSpec(image=container.image),
            tty=container.tty,
            stdin=container.interactive,
            args=args,
            envs=envs,
            working_dir=working_dir,
            labels=labels,
            mounts=mounts,
            linux=linux_config,
        )

    def _pull_image(self, context, container):
        # TODO(hongbin): add support for private registry
        response = self.image_stub.PullImage(
            api_pb2.PullImageRequest(
                image=api_pb2.ImageSpec(image=container.image)
            )
        )
        LOG.debug("image is pulled: %s", response)

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

    def _exec_in_container(self, container, cmd, timeout):
        """Run a command inside a container and return its exit code.

        This is the only way to observe a capsule from the outside. Nothing on
        the compute host can reach a capsule's address -- it lives on the
        tenant's OVN network, and a kata sandbox's namespace holds only a tap
        device -- so a probe has to run where the application is.
        """
        response = self.runtime_stub.ExecSync(
            api_pb2.ExecSyncRequest(
                container_id=container.container_id,
                cmd=cmd,
                timeout=timeout,
            )
        )
        return response.exit_code, response.stdout, response.stderr

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

        timeout = int(probe.get('timeoutSeconds') or 1)
        try:
            exit_code, _out, err = self._exec_in_container(
                container, list(cmd), timeout)
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
                    if self._check_probes(context, capsule, container):
                        changed = True
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

        state = (container.healthcheck or {}).get('k8s_probe_state') or {}
        changed = False

        # A startup probe gates the other two: until it passes, a slow-starting
        # application must not be restarted for failing a liveness check it was
        # never given time to satisfy.
        startup = probes.get('startupProbe')
        if startup and not state.get('startup_passed'):
            if self._probe_passed(container, startup, state, 'startup'):
                state['startup_passed'] = True
            else:
                if self._probe_failed_enough(startup, state, 'startup'):
                    LOG.info("Startup probe for container %s never passed; "
                             "restarting it", container.container_id)
                    self._restart_container(container)
                    state = {}
                self._save_probe_state(context, container, state)
                return True

        readiness = probes.get('readinessProbe')
        if readiness:
            ready = self._probe_passed(container, readiness, state, 'readiness')
            was_ready = state.get('ready', True)
            if ready != was_ready:
                state['ready'] = ready
                LOG.info("Container %(id)s readiness changed to %(ready)s",
                         {'id': container.container_id, 'ready': ready})
                changed = True

        liveness = probes.get('livenessProbe')
        if liveness:
            if self._probe_passed(container, liveness, state, 'liveness'):
                pass
            elif self._probe_failed_enough(liveness, state, 'liveness'):
                LOG.info("Liveness probe for container %s failed; restarting "
                         "it", container.container_id)
                self._restart_container(container)
                state = {}
                changed = True

        self._save_probe_state(context, container, state)
        return changed

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

    def _restart_container(self, container):
        """Stop a container so its restart policy brings it back.

        The sandbox and its network are left alone: restarting a container must
        not change the capsule's address, or every client would have to
        rediscover it.
        """
        try:
            self.runtime_stub.StopContainer(
                api_pb2.StopContainerRequest(
                    container_id=container.container_id, timeout=10))
            self.runtime_stub.StartContainer(
                api_pb2.StartContainerRequest(
                    container_id=container.container_id))
        except Exception as e:
            LOG.warning("Could not restart container %(id)s: %(err)s",
                        {'id': container.container_id, 'err': e})

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
