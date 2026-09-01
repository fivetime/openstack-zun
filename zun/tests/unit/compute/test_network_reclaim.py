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

"""The stale network sweep, as the compute manager runs it."""

from unittest import mock

from zun.compute import manager as manager_module
from zun.tests import base


class StaleNetworkSweepTest(base.TestCase):

    def setUp(self):
        super(StaleNetworkSweepTest, self).setUp()
        self.manager = manager_module.Manager.__new__(manager_module.Manager)
        self.manager.driver = mock.Mock()
        self.manager._last_report = {}
        self.config(reclaim_stale_networks=True, group='compute')
        self.config(reclaim_stale_networks_interval=900, group='compute')

    def test_the_driver_is_asked_when_due(self):
        self.manager.reclaim_stale_networks(mock.Mock())

        self.manager.driver.reclaim_stale_networks.assert_called_once()

    def test_not_again_before_the_interval(self):
        self.manager.reclaim_stale_networks(mock.Mock())
        self.manager.reclaim_stale_networks(mock.Mock())

        self.assertEqual(
            1, self.manager.driver.reclaim_stale_networks.call_count)

    def test_off_means_off(self):
        self.config(reclaim_stale_networks=False, group='compute')

        self.manager.reclaim_stale_networks(mock.Mock())

        self.manager.driver.reclaim_stale_networks.assert_not_called()

    def test_a_failing_sweep_does_not_take_the_service_down(self):
        self.manager.driver.reclaim_stale_networks.side_effect = (
            RuntimeError('dockerd hung'))

        self.manager.reclaim_stale_networks(mock.Mock())   # must not raise
