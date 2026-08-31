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

"""Restarting an exited container keeps what was written inside it.

The runtime will not start an exited container, so the driver builds a
replacement in the same sandbox. Between creating it and starting it,
both writable layers exist on the host as upper directories of the same
overlay chain -- the only moment the dead one's layer can be copied into
the new one. When that works, stop/start behaves as it does on docker;
when it does not, the driver falls back to what it always did and says
so in the status reason.
"""

from unittest import mock

from zun.container.cri import driver as cri_driver
from zun import objects
from zun.tests import base


class _Mount(object):
    def __init__(self, options, type='overlay', source=''):
        self.type = type
        self.options = options
        self.source = source


class RestartKeepsTheLayerTest(base.TestCase):

    def setUp(self):
        super(RestartKeepsTheLayerTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.driver.snapshot_stub = mock.Mock()
        # What the runtime would answer; see _runtime_snapshotter().
        self.driver._snapshotter = 'overlayfs'
        self.container = mock.Mock(uuid='u-1', container_id='old-id')

    def _mounts(self, upper):
        response = mock.Mock()
        response.mounts = [_Mount(['lowerdir=/low',
                                   'upperdir=%s' % upper,
                                   'workdir=/work'])]
        return response

    def _image_mounts(self, path, size):
        """What erofs answers for a layer it was asked to bound."""
        response = mock.Mock()
        response.mounts = [
            _Mount(['X-containerd.mkfs.fs=ext4',
                    'X-containerd.mkfs.size=%d' % size, 'rw', 'loop'],
                   type='mkfs/ext4', source=path),
            _Mount(['lowerdir=/low', 'upperdir={{ mount 0 }}/upper',
                    'workdir={{ mount 0 }}/work']),
        ]
        return response

    def test_the_upperdir_is_read_from_the_mount_options(self):
        self.driver.snapshot_stub.Mounts.return_value = self._mounts('/up/fs')

        self.assertEqual(('/up/fs', None),
                         self.driver._writable_layer_of('key-1'))

    def test_an_image_is_read_from_the_mount_that_makes_it(self):
        self.driver.snapshot_stub.Mounts.return_value = self._image_mounts(
            '/var/lib/containerd/erofs/snapshots/4/rwlayer.img', 1073741824)

        self.assertEqual(
            ('/var/lib/containerd/erofs/snapshots/4/rwlayer.img', 1073741824),
            self.driver._writable_layer_of('key-1'))

    def test_an_unfilled_upperdir_template_is_not_a_path(self):
        """It is only filled while the mount is live, which it is not here."""
        response = mock.Mock()
        response.mounts = [_Mount(['upperdir={{ mount 0 }}/upper'])]
        self.driver.snapshot_stub.Mounts.return_value = response

        self.assertEqual((None, None),
                         self.driver._writable_layer_of('key-1'))

    def test_the_lookup_names_the_configured_snapshotter(self):
        self.driver.snapshot_stub.Mounts.return_value = self._mounts('/up/fs')
        self.driver._writable_layer_of('key-1')

        request = self.driver.snapshot_stub.Mounts.call_args.args[0]
        self.assertEqual('overlayfs', request.snapshotter)
        self.assertEqual('key-1', request.key)

    def _restart(self, carried):
        create = mock.patch.object(self.driver, '_create_container')
        sandbox = mock.patch.object(self.driver, '_sandbox_of',
                                    return_value='sandbox-1')
        attempt = mock.patch.object(self.driver, '_attempt_of',
                                    return_value=0)
        remove = mock.patch.object(self.driver, '_remove_container')
        carry = mock.patch.object(self.driver, '_carry_writable_layer',
                                  return_value=carried)
        volumes = mock.patch.object(objects.VolumeMapping,
                                    'list_by_container', return_value=[])

        def created(context, capsule, container, volmaps, start, attempt):
            self.assertFalse(start)
            container.container_id = 'new-id'

        with create as mock_create, sandbox, attempt, \
                remove as mock_remove, carry as mock_carry, volumes:
            mock_create.side_effect = created
            self.driver._restart_exited({}, self.container)
        return mock_carry, mock_remove

    def test_a_carried_layer_leaves_no_scary_reason(self):
        carry, remove = self._restart(carried=True)

        carry.assert_called_once_with('old-id', 'new-id')
        remove.assert_called_once_with('old-id')
        self.assertIsNone(self.container.status_reason)
        self.driver.runtime_stub.StartContainer.assert_called_once()

    def test_the_copy_happens_before_the_replacement_starts(self):
        order = []
        with mock.patch.object(self.driver, '_carry_writable_layer',
                               side_effect=lambda *a: order.append('carry')):
            self.driver.runtime_stub.StartContainer.side_effect = \
                lambda *a, **k: order.append('start')
            self._restart_with_stubs()

        self.assertEqual(['carry', 'start'], order)

    def _restart_with_stubs(self):
        def created(context, capsule, container, volmaps, start, attempt):
            container.container_id = 'new-id'
        with mock.patch.object(self.driver, '_create_container',
                               side_effect=created), \
                mock.patch.object(self.driver, '_sandbox_of',
                                  return_value='sandbox-1'), \
                mock.patch.object(self.driver, '_attempt_of',
                                  return_value=0), \
                mock.patch.object(self.driver, '_remove_container'), \
                mock.patch.object(objects.VolumeMapping, 'list_by_container',
                                  return_value=[]):
            self.driver._restart_exited({}, self.container)

    def test_a_failed_carry_falls_back_and_says_so(self):
        carry, remove = self._restart(carried=False)

        self.assertEqual(self.driver.REBUILT_REASON,
                         self.container.status_reason)
        self.driver.runtime_stub.StartContainer.assert_called_once()

    def test_a_carry_failure_is_never_a_start_failure(self):
        """Best effort means the tenant's container still comes up."""
        with mock.patch.object(self.driver, '_writable_layer_of',
                               side_effect=RuntimeError('socket gone')):
            self.assertFalse(
                self.driver._carry_writable_layer('old-id', 'new-id'))


class CarryingAnImageLayerTest(base.TestCase):
    """Carrying a layer that lives in a bounded filesystem image.

    The image is the layer, filesystem and all, and the mount handler that
    would make one leaves an image that already exists alone -- so putting
    the dead container's image where the replacement's would go is the
    whole carry, and the replacement comes up with what was written.
    """

    def setUp(self):
        super(CarryingAnImageLayerTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.snapshot_stub = mock.Mock()
        self.driver._snapshotter = 'erofs'

    def _layers(self, dead, replacement):
        self.driver._writable_layer_of = mock.Mock(
            side_effect=[dead, replacement])

    def test_the_image_is_copied_over(self):
        self._layers(('/snap/1/rwlayer.img', 1024), ('/snap/2/rwlayer.img',
                                                     1024))

        with mock.patch.object(cri_driver.utils, 'execute') as execute:
            self.assertTrue(
                self.driver._carry_writable_layer('old', 'new'))

        execute.assert_called_once_with(
            'cp', '-a', '--sparse=always',
            '/snap/1/rwlayer.img', '/snap/2/rwlayer.img')

    def test_a_layer_bound_at_another_size_is_not_carried(self):
        """Larger would hand it past its disk, smaller would not hold it."""
        self._layers(('/snap/1/rwlayer.img', 2048), ('/snap/2/rwlayer.img',
                                                     1024))

        with mock.patch.object(cri_driver.utils, 'execute') as execute:
            self.assertFalse(
                self.driver._carry_writable_layer('old', 'new'))

        execute.assert_not_called()

    def test_a_directory_is_not_poured_into_an_image(self):
        """The node was reconfigured between the two incarnations."""
        self._layers(('/up/fs', None), ('/snap/2/rwlayer.img', 1024))

        with mock.patch.object(cri_driver.utils, 'execute') as execute:
            self.assertFalse(
                self.driver._carry_writable_layer('old', 'new'))

        execute.assert_not_called()
