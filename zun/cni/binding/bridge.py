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

import os
import time

from neutron_lib import constants as neutron_constants
from oslo_log import log
from pyroute2.netlink import exceptions as netlink_exceptions

from zun.cni.binding import base as b_base
from zun.cni import utils as cni_utils
from zun.common import privileged
from zun.network import linux_net as net_utils


LOG = log.getLogger(__name__)

# How many times to read the host's links again when the read was interrupted,
# and the wait before the first of them, doubling.
DUMP_ATTEMPTS = 6
DUMP_RETRY_WAIT = 0.1


@privileged.cni.entrypoint
def base_bridge_connect(vif_dict, ifname, netns, container_id, **kwargs):
    _connect_retrying(vif_dict, ifname, netns, container_id, **kwargs)


def _connect_retrying(vif_dict, ifname, netns, container_id, **kwargs):
    """Connect, reading the host's links again if they changed meanwhile.

    The kernel marks a dump NLM_F_DUMP_INTR when the table changed while it
    was being read, and pyroute2 raises for it. That is the kernel's way of
    saying to read again, and it happens whenever another veth is being made
    on the host: two sandboxes starting together on a node, which is what a
    rollout and a scaled-up Deployment do, and the capsule went to Error.

    Starting over is safe: a connect begins by removing what an earlier
    attempt left.
    """
    for attempt in range(DUMP_ATTEMPTS):
        try:
            return _connect(vif_dict, ifname, netns, container_id,
                            retrying=attempt > 0, **kwargs)
        except netlink_exceptions.NetlinkDumpInterrupted:
            if attempt == DUMP_ATTEMPTS - 1:
                raise
            LOG.debug('Reading links was interrupted connecting %s; trying '
                      'again', vif_dict['vif_name'])
            time.sleep(DUMP_RETRY_WAIT * 2 ** attempt)


def _connect(vif_dict, ifname, netns, container_id, retrying=False, **kwargs):
    host_ifname = vif_dict['vif_name']
    mtu = vif_dict['network']['mtu']
    address = vif_dict['address']
    bridge_name = vif_dict['bridge_name']

    with b_base.get_ipdb() as h_ipdb:
        if host_ifname in h_ipdb.interfaces:
            # NOTE(dulek): This most likely means that we already run
            #              connect for this iface and there's a leftover
            #              host-side vif. Let's remove it, its peer should
            #              get deleted automatically by the kernel.
            LOG.debug('Found leftover host vif %s. Removing it before '
                      'connecting.', host_ifname)
            with h_ipdb.interfaces[host_ifname] as h_iface:
                h_iface.remove()

    if retrying and netns:
        # The pair not yet moved to the host, which the removal above misses.
        with b_base.get_ipdb(netns) as c_ipdb:
            if ifname in c_ipdb.interfaces:
                with c_ipdb.interfaces[ifname] as c_iface:
                    c_iface.remove()

    if mtu:
        interface_mtu = mtu
    else:
        LOG.info("Default mtu %(mtu)s is used for interface, "
                 "for mtu of network if configured with 0",
                 {"mtu": neutron_constants.DEFAULT_NETWORK_MTU})
        interface_mtu = neutron_constants.DEFAULT_NETWORK_MTU

    with b_base.get_ipdb(netns) as c_ipdb:
        with c_ipdb.create(ifname=ifname, peer=host_ifname,
                           kind='veth') as c_iface:
            c_iface.mtu = interface_mtu
            c_iface.address = address
            c_iface.up()

        if netns:
            with c_ipdb.interfaces[host_ifname] as h_iface:
                h_iface.net_ns_pid = os.getpid()

    with b_base.get_ipdb() as h_ipdb:
        with h_ipdb.interfaces[host_ifname] as h_iface:
            h_iface.mtu = interface_mtu
            h_iface.up()

    if kwargs.get('driver') == 'bridge':
        with b_base.get_ipdb() as h_ipdb:
            with h_ipdb.interfaces[bridge_name] as h_br:
                h_br.add_port(host_ifname)


class BaseBridgeDriver(b_base.BaseBindingDriver):

    def connect(self, vif, ifname, netns, container_id, **kwargs):
        vif_dict = cni_utils.osvif_vif_to_dict(vif)
        base_bridge_connect(vif_dict, ifname, netns, container_id, **kwargs)

    def disconnect(self, vif, ifname, netns, container_id):
        pass


class BridgeDriver(BaseBridgeDriver):

    def connect(self, vif, ifname, netns, container_id):
        super(BridgeDriver, self).connect(vif, ifname, netns, container_id,
                                          driver='bridge')

    def disconnect(self, vif, ifname, netns, container_id):
        # NOTE(ivc): veth pair is destroyed automatically along with the
        # container namespace
        pass


class VIFOpenVSwitchDriver(BaseBridgeDriver):

    def connect(self, vif, ifname, netns, container_id):
        super(VIFOpenVSwitchDriver, self).connect(vif, ifname, netns,
                                                  container_id)
        # FIXME(irenab) use container_uuid (neutron port device_id)
        instance_id = 'zun'
        net_utils.create_ovs_vif_port(vif.bridge_name, vif.vif_name,
                                      vif.port_profile.interface_id,
                                      vif.address, instance_id)

    def disconnect(self, vif, ifname, netns, container_id):
        super(VIFOpenVSwitchDriver, self).disconnect(vif, ifname, netns,
                                                     container_id)
        net_utils.delete_ovs_vif_port(vif.bridge_name, vif.vif_name)
