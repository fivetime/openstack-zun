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

"""A create that fails leaves nothing behind.

The sandbox and its Neutron port exist before the first member container
does. Nothing above the driver will ever remove them once the create has
failed: the manager unsets the host, and the API then deletes the capsule by
dropping its rows without telling any compute node. Measured on the testbed:
9 sandboxes running for three weeks, each holding a port and an address,
none of them known to the database.
"""

from unittest import mock

from zun.container.cri import driver as cri_driver
from zun.tests import base


class CreateCleanupTest(base.TestCase):

    def setUp(self):
        super(CreateCleanupTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.context = mock.Mock()
        self.member = mock.Mock(uuid='m-1')
        self.capsule = mock.Mock(uuid='c-1', container_id=None,
                                 addresses=None, containers=[self.member],
                                 init_containers=[])

        def sandbox_up(context, capsule, requested_networks, labels=None):
            capsule.addresses = {'net-1': [{'port': 'p-1',
                                            'preserve_on_delete': False}]}
            capsule.container_id = 'sb-1'

        self.create_sandbox = self._patch('_create_pod_sandbox',
                                          side_effect=sandbox_up)
        self.create_container = self._patch('_create_container')
        self.delete_sandbox = self._patch('_delete_sandbox')
        self.delete_ports = self._patch('_delete_neutron_ports')

    def _patch(self, name, **kwargs):
        patcher = mock.patch.object(cri_driver.CriDriver, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _create(self):
        return self.driver.create_capsule(self.context, self.capsule, {},
                                          [{'network': 'net-1'}], {})

    def test_a_member_that_fails_takes_the_sandbox_and_port_with_it(self):
        self.create_container.side_effect = RuntimeError('no such image')

        self.assertRaisesRegex(RuntimeError, 'no such image', self._create)

        self.delete_sandbox.assert_called_once_with(self.context, self.capsule,
                                                    'sb-1')
        self.delete_ports.assert_called_once_with(self.context, self.capsule)
        # The row about to be saved as ERROR names nothing that is gone.
        self.assertIsNone(self.capsule.container_id)
        self.assertEqual({}, self.capsule.addresses)

    def test_a_sandbox_that_never_came_up_still_releases_its_port(self):
        def port_then_boom(context, capsule, requested_networks, labels=None):
            capsule.addresses = {'net-1': [{'port': 'p-1',
                                            'preserve_on_delete': False}]}
            raise RuntimeError('RunPodSandbox: no space left')

        self.create_sandbox.side_effect = port_then_boom

        self.assertRaisesRegex(RuntimeError, 'no space left', self._create)

        # No sandbox id, so nothing to remove -- and no removal attempted,
        # since one would fail and drown the real error.
        self.delete_sandbox.assert_not_called()
        self.delete_ports.assert_called_once_with(self.context, self.capsule)

    def test_a_failing_cleanup_neither_hides_the_error_nor_stops(self):
        self.create_container.side_effect = RuntimeError('no such image')
        self.delete_sandbox.side_effect = RuntimeError('shim is gone')

        # The tenant reads the create's error, not the cleanup's.
        self.assertRaisesRegex(RuntimeError, 'no such image', self._create)

        # The port is released even though the sandbox would not go, and the
        # row keeps naming the sandbox that is still there.
        self.delete_ports.assert_called_once_with(self.context, self.capsule)
        self.assertEqual('sb-1', self.capsule.container_id)
        self.assertEqual({}, self.capsule.addresses)

    def test_a_create_that_succeeds_removes_nothing(self):
        self._create()

        self.delete_sandbox.assert_not_called()
        self.delete_ports.assert_not_called()
        self.assertEqual('sb-1', self.capsule.container_id)


class OwnerLabelTest(base.TestCase):
    """Every sandbox this driver makes says whose it is.

    The orphan sweep reaps only sandboxes carrying the owner label whose row
    is gone, and leaves an unlabelled one alone as the kubelet's. A capsule's
    sandbox used to carry no label, so the sweep could never have reached the
    ones this driver leaked, whatever it was set to.
    """

    def setUp(self):
        super(OwnerLabelTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.driver.runtime_stub.RunPodSandbox.return_value = mock.Mock(
            pod_sandbox_id='sb-1')
        self.capsule = mock.Mock(uuid='c-1', runtime='runc', dns=None,
                                 dns_search=None, annotations={})
        for name in ('_write_cni_metadata', '_apply_pod_ceiling',
                     '_apply_host_io_limits'):
            p = mock.patch.object(cri_driver.CriDriver, name)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(cri_driver.CriDriver, '_get_sandbox_config')
        self.sandbox_config = p.start()
        self.addCleanup(p.stop)
        # A real message: the request the sandbox goes into is protobuf, and
        # protobuf will not take a mock for a field.
        self.sandbox_config.return_value = cri_driver.api_pb2.PodSandboxConfig()

    def _labels_sent(self):
        return self.sandbox_config.call_args.kwargs['labels']

    def test_a_capsule_sandbox_is_labelled_with_its_owner(self):
        self.driver._create_pod_sandbox(mock.Mock(), self.capsule, [])
        self.assertEqual({cri_driver.CriDriver.OWNER_LABEL: 'c-1'},
                         self._labels_sent())

    def test_labels_a_caller_asks_for_are_kept(self):
        self.driver._create_pod_sandbox(mock.Mock(), self.capsule, [],
                                        labels={'tier': 'gold'})
        self.assertEqual({cri_driver.CriDriver.OWNER_LABEL: 'c-1',
                          'tier': 'gold'}, self._labels_sent())
