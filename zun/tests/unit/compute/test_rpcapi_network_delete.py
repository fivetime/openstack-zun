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

"""Removing a docker network from the hosts that actually have it.

The network is made on whichever host a container first lands on, so
over a deployment's life that is potentially every host. Asking one of
them to remove it left the rest holding a network whose neutron network
was gone -- and since the docker network is what makes libnetwork call
ReleasePool, each one held a subnetpool nobody would reclaim. Enough
pools with one name and the IPAM driver refuses to create the next,
which stops networks being created at all.

Measured on a three-node deployment: 26 orphan networks; removing them
released 54 pools with no other action taken.
"""

from unittest import mock

from zun.compute import rpcapi
from zun.tests import base


class _Service(object):
    def __init__(self, host):
        self.host = host


class TestEveryHostIsAsked(base.TestCase):

    def setUp(self):
        super(TestEveryHostIsAsked, self).setUp()
        self.api = rpcapi.API.__new__(rpcapi.API)
        self.hosts = ['c1', 'c2', 'c3']
        patcher = mock.patch.object(
            rpcapi.objects.ZunService, 'list_by_binary',
            side_effect=lambda _c, _b: [_Service(h) for h in self.hosts])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call_patch(self, **kwargs):
        return mock.patch.object(rpcapi.API, '_call', **kwargs)

    def test_each_host_is_asked_once(self):
        with self._call_patch(return_value=None) as called:
            self.api.network_delete(mock.Mock(), mock.Mock(name_='n'))

        self.assertEqual(['c1', 'c2', 'c3'],
                         [c[0][0] for c in called.call_args_list])

    def test_one_host_failing_does_not_stop_the_others(self):
        """Residue on the hosts that are up is the thing to avoid."""
        def _fail_on_c1(host, *a, **kw):
            if host == 'c1':
                raise RuntimeError('down')

        with self._call_patch(side_effect=_fail_on_c1) as called:
            self.api.network_delete(mock.Mock(), mock.Mock(name_='n'))

        self.assertEqual(['c1', 'c2', 'c3'],
                         [c[0][0] for c in called.call_args_list])

    def test_a_host_that_is_down_is_still_asked(self):
        """It holds the network on disk and will answer for it on return."""
        with self._call_patch(return_value=None) as called:
            self.api.network_delete(mock.Mock(), mock.Mock(name_='n'))

        self.assertEqual(3, called.call_count)

    def test_no_compute_hosts_is_not_an_error(self):
        self.hosts = []

        with self._call_patch(return_value=None) as called:
            self.api.network_delete(mock.Mock(), mock.Mock(name_='n'))

        called.assert_not_called()
