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

"""The pod-level cgroup ceiling, written by the driver because kubelet is not
here to write it.

Kubernetes enforces a pod's total at the pod cgroup, which kubelet creates
and writes before the runtime is called. In this chain nothing wrote it:
measured, a runsc sandbox for a capsule limited to 1 cpu and 256Mi sat under
cpu.max=max memory.max=max and a 512MB allocation inside ran to completion --
gVisor holds every container inside one sandbox process, so the pod cgroup is
the only place a ceiling can bite.
"""

import os
import tempfile
from unittest import mock

from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


class PodCeilingTest(base.TestCase):

    def setUp(self):
        super(PodCeilingTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.root = tempfile.mkdtemp()
        self._patch(mock.patch.object(cri_driver.CriDriver, 'CGROUP_ROOT',
                                      self.root))
        member = mock.Mock(cpu=1, memory=256)
        self.capsule = mock.Mock(uuid='u-1', containers=[member],
                                 init_containers=[])

    def _patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _pod_dir(self):
        return os.path.join(
            self.root,
            cri_driver.CriDriver._sandbox_cgroup_parent(
                self.capsule).lstrip('/'))

    def _read(self, name):
        with open(os.path.join(self._pod_dir(), name)) as f:
            return f.read()

    def test_ceiling_is_written_for_a_cgroup_runtime(self):
        self.driver._apply_pod_ceiling(self.capsule, 'runsc')

        self.assertEqual('100000 100000', self._read('cpu.max'))
        self.assertEqual(str(256 * 1024 * 1024), self._read('memory.max'))
        # Swap off, the kubelet default: the memory ceiling must not be
        # quietly widened by the host's swap.
        self.assertEqual('0', self._read('memory.swap.max'))

    def test_a_vm_runtime_is_left_alone(self):
        # kata enforces inside the guest; capping the VMM at the members'
        # sum would starve the VMM itself.
        self.driver._apply_pod_ceiling(self.capsule, 'kata-qemu')
        self.assertFalse(os.path.exists(self._pod_dir()))

    def test_nothing_limited_writes_nothing(self):
        self.capsule.containers = [mock.Mock(cpu=None, memory=None)]
        self.driver._apply_pod_ceiling(self.capsule, 'runsc')
        self.assertFalse(os.path.exists(self._pod_dir()))

    def test_a_ceiling_that_cannot_land_fails_closed(self):
        # A ceiling that was asked for and could not be written is the
        # tenant running unlimited -- the hole this exists to close.
        blocker = os.path.join(self.root, 'zun.slice')
        with open(blocker, 'w') as f:
            f.write('a file where the slice directory must go')

        self.assertRaises(exception.ZunException,
                          self.driver._apply_pod_ceiling,
                          self.capsule, 'runsc')
