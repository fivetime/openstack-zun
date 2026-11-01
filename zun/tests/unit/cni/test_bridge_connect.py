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

"""Two sandboxes starting together on a node interrupt each other's dumps."""

from unittest import mock

from pyroute2.netlink import exceptions as netlink_exceptions

from zun.cni.binding import bridge
from zun.tests import base


class ConnectRetryTest(base.TestCase):

    VIF = {'vif_name': 'tap1', 'address': 'aa:bb', 'bridge_name': 'br-int',
           'network': {'mtu': 1450}}

    def setUp(self):
        super(ConnectRetryTest, self).setUp()
        sleep = mock.patch('time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    @staticmethod
    def _ipdb(*interfaces):
        ipdb = mock.MagicMock()
        ipdb.__enter__.return_value = ipdb
        known = {name: mock.MagicMock() for name in interfaces}
        for iface in known.values():
            iface.__enter__.return_value = iface
        ipdb.interfaces = known
        return ipdb

    @staticmethod
    def _raise_or(answers, ipdb):
        if answers:
            raise answers.pop()
        return ipdb

    def _connect(self, get_ipdb):
        with mock.patch.object(bridge.b_base, 'get_ipdb', get_ipdb):
            bridge._connect_retrying(self.VIF, 'eth0', '/ns', 'c-1')

    def test_an_interrupted_read_is_read_again(self):
        interrupted = netlink_exceptions.NetlinkDumpInterrupted()
        answers = [interrupted, interrupted]
        ipdb = self._ipdb('tap1')
        get_ipdb = mock.Mock(
            side_effect=lambda *a: self._raise_or(answers, ipdb))
        self._connect(get_ipdb)
        self.assertEqual(2, self.sleep.call_count)

    def test_it_gives_up_in_the_end(self):
        get_ipdb = mock.Mock(
            side_effect=netlink_exceptions.NetlinkDumpInterrupted())
        self.assertRaises(netlink_exceptions.NetlinkDumpInterrupted,
                          self._connect, get_ipdb)
        self.assertEqual(bridge.DUMP_ATTEMPTS, get_ipdb.call_count)

    def test_any_other_failure_is_not_read_again(self):
        get_ipdb = mock.Mock(side_effect=OSError('no'))
        self.assertRaises(OSError, self._connect, get_ipdb)
        self.assertEqual(1, get_ipdb.call_count)

    def test_a_retry_removes_what_the_last_attempt_left_in_the_netns(self):
        leftover = self._ipdb('eth0')
        with mock.patch.object(bridge.b_base, 'get_ipdb',
                               return_value=leftover), \
                mock.patch.object(bridge, 'LOG'):
            leftover.interfaces['tap1'] = mock.MagicMock()
            try:
                bridge._connect(self.VIF, 'eth0', '/ns', 'c-1', retrying=True)
            except Exception:
                pass
        leftover.interfaces['eth0'].remove.assert_called_once()

    def test_a_first_attempt_leaves_the_netns_alone(self):
        ipdb = self._ipdb('eth0', 'tap1')
        with mock.patch.object(bridge.b_base, 'get_ipdb', return_value=ipdb):
            bridge._connect(self.VIF, 'eth0', '/ns', 'c-1')
        ipdb.interfaces['eth0'].remove.assert_not_called()
