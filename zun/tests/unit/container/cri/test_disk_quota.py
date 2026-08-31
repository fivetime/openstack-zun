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

"""Whether this node can hold a container to the disk it asked for.

The answer is the snapshotter behind the node's runtime handler, and it is
per handler rather than per node: containerd ships `kata-fc` on devmapper
beside handlers left on overlayfs. Only erofs sizes a writable layer per
container, so only erofs reads as supporting a quota -- everything else,
including a runtime whose config cannot be read, reads as no quota, which
is what makes the compute manager refuse a container that asks for one
instead of accepting `disk` and silently dropping it.
"""

import json
from unittest import mock

import grpc

from zun.container.cri import driver
from zun.tests import base


def _status(runtimes):
    """A CRI Status response carrying the runtime config containerd reports."""
    return mock.Mock(info={'config': json.dumps({
        'containerd': {'runtimes': runtimes}})})


class _Driver(driver.CriDriver):
    """A driver with the gRPC channel left out."""

    def __init__(self, status):
        self.runtime_stub = mock.Mock()
        self.runtime_stub.Status.return_value = status
        self._snapshotter = None


class TestNodeSupportsDiskQuota(base.TestCase):

    def setUp(self):
        super(TestNodeSupportsDiskQuota, self).setUp()
        self.config(container_runtime='kata-qemu')

    def test_erofs_supports_it(self):
        d = _Driver(_status({'kata-qemu': {'snapshotter': 'erofs'}}))

        self.assertTrue(d.node_support_disk_quota())

    def test_overlayfs_does_not(self):
        d = _Driver(_status({'kata-qemu': {'snapshotter': 'overlayfs'}}))

        self.assertFalse(d.node_support_disk_quota())

    def test_devmapper_does_not(self):
        """One node-wide device size is not a per-container quota."""
        d = _Driver(_status({'kata-qemu': {'snapshotter': 'devmapper'}}))

        self.assertFalse(d.node_support_disk_quota())

    def test_a_handler_on_the_default_snapshotter_does_not(self):
        """Status does not report the default, so it reads as unknown."""
        d = _Driver(_status({'kata-qemu': {'snapshotter': ''}}))

        self.assertFalse(d.node_support_disk_quota())

    def test_the_answer_is_read_for_this_nodes_handler_only(self):
        """Another handler being on erofs says nothing about this one."""
        d = _Driver(_status({'kata-qemu': {'snapshotter': 'overlayfs'},
                             'kata-fc': {'snapshotter': 'erofs'}}))

        self.assertFalse(d.node_support_disk_quota())

    def test_an_unknown_handler_does_not(self):
        d = _Driver(_status({'runc': {'snapshotter': 'erofs'}}))

        self.assertFalse(d.node_support_disk_quota())

    def test_an_unreadable_runtime_does_not(self):
        d = _Driver(None)
        d.runtime_stub.Status.side_effect = grpc.RpcError('no answer')

        self.assertFalse(d.node_support_disk_quota())

    def test_the_runtime_is_asked_once(self):
        d = _Driver(_status({'kata-qemu': {'snapshotter': 'erofs'}}))

        d.node_support_disk_quota()
        d.node_support_disk_quota()

        self.assertEqual(1, d.runtime_stub.Status.call_count)


class TestSnapshotAnnotations(base.TestCase):

    def setUp(self):
        super(TestSnapshotAnnotations, self).setUp()
        self.config(container_runtime='kata-qemu')
        self.erofs = _Driver(_status({'kata-qemu': {'snapshotter': 'erofs'}}))
        self.overlay = _Driver(
            _status({'kata-qemu': {'snapshotter': 'overlayfs'}}))

    def test_disk_becomes_the_label_in_bytes(self):
        """`disk` is GiB, the label is bytes."""
        container = mock.Mock(disk=20, uuid='u')

        self.assertEqual(
            {driver.MAX_SIZE_LABEL: str(20 * 1024 ** 3)},
            self.erofs._snapshot_annotations(container))

    def test_no_disk_means_no_label(self):
        """The snapshotter's own default applies; 0 would mean no limit."""
        container = mock.Mock(disk=0, uuid='u')

        self.assertEqual({}, self.erofs._snapshot_annotations(container))

    def test_no_label_where_it_would_be_ignored(self):
        """Emitting one the snapshotter drops is the silent drop to avoid."""
        container = mock.Mock(disk=20, uuid='u')

        self.assertEqual({}, self.overlay._snapshot_annotations(container))
