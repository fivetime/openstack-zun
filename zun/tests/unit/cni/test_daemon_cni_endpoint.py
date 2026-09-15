#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
#    implied. See the License for the specific language governing
#    permissions and limitations under the License.

"""The daemon answers a whole CNI call, so the host binary needs no Python.

The plugin containerd executes on the host used to be a Python console script
that turned the daemon's VIF into a CNI result itself. That only works on a
host that has this Python environment -- the testbed does, a node whose daemon
runs in a container does not. /cni does the conversion in the daemon.
"""

from unittest import mock

from os_vif import objects as osv_objects
from oslo_serialization import jsonutils

from zun.cni import api as cni_api
from zun.cni.daemon import service
from zun.common import consts
from zun.common import exception
from zun.tests import base


def _vif():
    osv_objects.register_all()
    subnet = osv_objects.subnet.Subnet(
        cidr='192.168.1.0/24', gateway='192.168.1.1', dns=['192.168.1.2'],
        ips=osv_objects.fixed_ip.FixedIPList(
            objects=[osv_objects.fixed_ip.FixedIP(address='192.168.1.30')]),
        routes=osv_objects.route.RouteList(objects=[
            osv_objects.route.Route(cidr='10.0.0.0/8',
                                    gateway='192.168.1.254')]))
    network = osv_objects.network.Network(
        subnets=osv_objects.subnet.SubnetList(objects=[subnet]))
    return osv_objects.vif.VIFOpenVSwitch(
        id='5b0f6c0e-1d8e-4b54-a1f1-4c3b0f2b8a11',
        address='fa:16:3e:12:34:56', network=network)


class CNIEndpointTest(base.TestCase):

    def setUp(self):
        super(CNIEndpointTest, self).setUp()
        self.plugin = mock.Mock()
        self.client = service.DaemonServer(self.plugin).application \
            .test_client()

    def _call(self, command, **extra):
        body = {
            'CNI_COMMAND': command,
            'CNI_CONTAINERID': 'sandbox-1',
            'CNI_NETNS': '/var/run/netns/cni-1',
            'CNI_IFNAME': 'eth0',
            'CNI_ARGS': 'K8S_POD_NAME=capsule-uuid',
            'CNI_PATH': '/opt/cni/bin',
            'config_zun': {'cniVersion': '0.3.1', 'name': 'zun',
                           'type': 'zun-cni'},
        }
        body.update(extra)
        return self.client.post('/cni', json=body)

    def test_add_answers_with_the_cni_result_not_a_vif(self):
        self.plugin.add.return_value = _vif()

        resp = self._call('ADD')

        self.assertEqual(200, resp.status_code)
        out = jsonutils.loads(resp.data)
        self.assertEqual('0.3.1', out['cniVersion'])
        self.assertEqual([{'name': 'eth0', 'mac': 'fa:16:3e:12:34:56',
                           'sandbox': 'sandbox-1'}], out['interfaces'])
        self.assertEqual([{'version': '4', 'address': '192.168.1.30/24',
                           'interface': 0, 'gateway': '192.168.1.1'}],
                         out['ips'])
        self.assertEqual([{'dst': '10.0.0.0/8', 'gw': '192.168.1.254'}],
                         out['routes'])
        self.assertEqual({'nameservers': ['192.168.1.2']}, out['dns'])
        self.assertNotIn('versioned_object.data', resp.data.decode())

    def test_add_result_matches_what_the_python_plugin_produces(self):
        vif = _vif()
        self.plugin.add.return_value = vif
        runner = cni_api.CNIDaemonizedRunner()
        params = {'CNI_IFNAME': 'eth0', 'CNI_CONTAINERID': 'sandbox-1'}

        out = jsonutils.loads(self._call('ADD').data)
        out.pop('cniVersion')

        # Compared as the plugin emits it: through jsonutils, onto stdout.
        # The result holds os-vif address objects until it is serialized.
        emitted = jsonutils.loads(jsonutils.dumps(runner._vif_data(vif,
                                                                   params)))
        self.assertEqual(emitted, out)

    def test_an_add_whose_port_never_activates_is_a_cni_timeout(self):
        self.plugin.add.side_effect = exception.ResourceNotReady(
            resource='capsule-uuid')

        resp = self._call('ADD')

        self.assertEqual(504, resp.status_code)
        self.assertEqual(consts.CNI_TIMEOUT_CODE,
                         jsonutils.loads(resp.data)['code'])

    def test_a_failing_add_is_a_cni_error_with_the_reason(self):
        self.plugin.add.side_effect = RuntimeError('ovsdb unreachable')

        resp = self._call('ADD')

        self.assertEqual(500, resp.status_code)
        out = jsonutils.loads(resp.data)
        self.assertEqual(consts.CNI_EXCEPTION_CODE, out['code'])
        self.assertIn('ovsdb unreachable', out['msg'])

    def test_del_succeeds_with_no_output(self):
        resp = self._call('DEL')

        self.assertEqual(200, resp.status_code)
        self.assertEqual(b'', resp.data)
        self.plugin.delete.assert_called_once()

    def test_del_of_something_already_gone_still_succeeds(self):
        self.plugin.delete.side_effect = exception.ResourceNotReady(
            resource='capsule-uuid')

        self.assertEqual(200, self._call('DEL').status_code)

    def test_a_failing_del_is_an_error(self):
        self.plugin.delete.side_effect = RuntimeError('boom')

        self.assertEqual(500, self._call('DEL').status_code)

    def test_version(self):
        # A runtime sends VERSION with no CNI_ARGS and no container at all.
        resp = self.client.post('/cni', json={'CNI_COMMAND': 'VERSION',
                                              'config_zun': {}})
        self.assertEqual(200, resp.status_code)
        out = jsonutils.loads(resp.data)

        self.assertEqual(['0.3.1'], out['supportedVersions'])
        self.plugin.add.assert_not_called()

    def test_a_request_that_is_not_cni_is_refused(self):
        resp = self.client.post('/cni', json={'CNI_COMMAND': 'ADD'})

        self.assertEqual(400, resp.status_code)
        self.plugin.add.assert_not_called()

    def test_an_unknown_command_is_refused(self):
        resp = self._call('CHECK')

        self.assertEqual(400, resp.status_code)
        self.assertIn('CHECK', jsonutils.loads(resp.data)['msg'])

    def test_the_old_endpoints_are_unchanged(self):
        self.plugin.add.return_value = _vif()

        resp = self.client.post('/addNetwork', json={
            'CNI_COMMAND': 'ADD', 'CNI_ARGS': 'K8S_POD_NAME=x',
            'CNI_IFNAME': 'eth0', 'CNI_CONTAINERID': 'c', 'config_zun': {}})

        self.assertEqual(202, resp.status_code)
        self.assertIn('versioned_object.data', resp.data.decode())
