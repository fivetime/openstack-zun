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

"""No call to the runtime waits forever.

Every call runs on one of twenty worker threads, and a call about a sandbox
whose shim has stopped answering never returns. Measured on the testbed:
nineteen threads each stuck in a node-wide ListContainerStats, one in a
StopPodSandbox, and every exec and status call for the node's healthy
capsules timing out behind them -- while containerd, asked directly,
answered for those capsules in a second.
"""

from unittest import mock

from zun.container.cri import driver as cri_driver
from zun.criapi import api_pb2
from zun.tests import base


class DeadlineTest(base.TestCase):

    def setUp(self):
        super(DeadlineTest, self).setUp()
        self.stub = mock.Mock()
        self.bounded = cri_driver._Bounded(self.stub)
        self.config(cri_read_timeout=15, cri_request_timeout=120,
                   cri_pull_timeout=1800)

    def _deadline(self, method, request=None):
        getattr(self.bounded, method)(request or mock.Mock(spec=[]))
        return getattr(self.stub, method).call_args.kwargs['timeout']

    def test_the_call_that_hung_the_node_is_bounded_and_short(self):
        self.assertEqual(15, self._deadline(
            'ListContainerStats', api_pb2.ListContainerStatsRequest()))

    def test_every_read_is_bounded_by_the_read_timeout(self):
        for method in ('Status', 'ListPodSandbox', 'PodSandboxStatus',
                       'ListContainers', 'ContainerStatus', 'ContainerStats',
                       'PodSandboxStats', 'ListPodSandboxStats',
                       'ListImages', 'Get', 'Mounts'):
            self.assertEqual(15, self._deadline(method), method)

    def test_a_change_is_bounded_like_the_kubelet_bounds_it(self):
        for method in ('RunPodSandbox', 'StopPodSandbox', 'RemovePodSandbox',
                       'CreateContainer', 'StartContainer', 'RemoveContainer',
                       'UpdateContainerResources', 'Kill', 'Pause'):
            self.assertEqual(120, self._deadline(method), method)

    def test_a_stop_is_allowed_its_grace_on_top(self):
        request = api_pb2.StopContainerRequest(container_id='c', timeout=30)

        self.assertEqual(150, self._deadline('StopContainer', request))

    def test_a_pull_gets_the_long_bound(self):
        self.assertEqual(1800, self._deadline('PullImage'))

    def test_a_call_nobody_listed_is_still_bounded(self):
        self.assertEqual(120, self._deadline('SomethingNew'))

    def test_a_deadline_the_caller_chose_is_kept(self):
        self.bounded.ExecSync('req', timeout=45)

        self.assertEqual(45, self.stub.ExecSync.call_args.kwargs['timeout'])

    def test_what_the_stub_returns_comes_back(self):
        self.stub.Version.return_value = 'v1'

        self.assertEqual('v1', self.bounded.Version('req'))

    def test_the_deadline_follows_the_configuration(self):
        self.config(cri_read_timeout=3)

        self.assertEqual(3, self._deadline('ListContainerStats'))


class DriverStubsAreBoundedTest(base.TestCase):

    def _driver(self):
        with mock.patch.object(cri_driver.grpc, 'insecure_channel'):
            with mock.patch.object(cri_driver.img_driver,
                                   'load_image_driver'):
                with mock.patch.object(cri_driver.CONF, 'image_driver_list',
                                       []):
                    return cri_driver.CriDriver()

    def test_every_stub_that_runs_on_a_thread_has_deadlines(self):
        driver = self._driver()

        for name in ('runtime_stub', 'image_stub', 'task_stub',
                     'snapshot_stub'):
            self.assertIsInstance(getattr(driver, name)._obj,
                                  cri_driver._Bounded, name)
