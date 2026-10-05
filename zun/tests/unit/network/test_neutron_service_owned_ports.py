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

"""Ports zun made are deleted with zun's own credentials.

Under Neutron's secure RBAC (2026.2 on the testbed) a project member may not
delete a port whose device_owner starts with ``compute:`` -- every port zun
creates is ``compute:zun``. The delete ran with the tenant's context, Neutron
answered "rule:delete_port is disallowed by policy", and each deleted capsule
stayed behind in Error holding its port and its address.
"""

from unittest import mock

from zun.network import neutron
from zun.tests import base


class ServiceOwnedPortTest(base.TestCase):

    def setUp(self):
        super(ServiceOwnedPortTest, self).setUp()
        p = mock.patch.object(neutron.clients, 'OpenStackClients')
        self.clients = p.start()
        self.addCleanup(p.stop)
        self.tenant = mock.Mock(name='tenant-client')
        self.admin = mock.Mock(name='admin-client')
        self.clients.side_effect = lambda ctx: mock.Mock(
            neutron=mock.Mock(return_value=(
                self.admin if ctx is self.admin_ctx else self.tenant)))
        self.admin_ctx = mock.Mock(name='admin-context')
        p = mock.patch.object(neutron.zun_context, 'get_admin_context',
                              return_value=self.admin_ctx)
        p.start()
        self.addCleanup(p.stop)
        self.api = neutron.NeutronAPI(mock.Mock(name='tenant-context'))

    def test_a_port_zun_made_is_deleted_with_zuns_credentials(self):
        self.api.delete_or_unbind_ports({'p-1'}, {'p-1'})

        self.admin.delete_port.assert_called_once_with('p-1')
        self.tenant.delete_port.assert_not_called()

    def test_a_port_the_user_brought_is_unbound_not_deleted(self):
        with mock.patch.object(self.api, '_unbind_port') as unbind:
            self.api.delete_or_unbind_ports({'p-user'}, set())

        unbind.assert_called_once_with('p-user')
        self.admin.delete_port.assert_not_called()
        self.tenant.delete_port.assert_not_called()

    def test_a_plain_delete_still_uses_the_callers_credentials(self):
        self.api.delete_port('p-2')

        self.tenant.delete_port.assert_called_once_with('p-2')
        self.admin.delete_port.assert_not_called()
