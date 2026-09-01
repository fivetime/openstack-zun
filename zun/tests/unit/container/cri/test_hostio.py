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

"""Block IO limits, which the runtime interface cannot carry.

LinuxContainerResources has cpu, memory and swap and nothing else, so these
are written on the host instead -- on the cgroup the sandbox's processes are
accounted in, which for a VM runtime is where the VMM's own traffic to the
node's disk is counted. That is the traffic worth limiting: the guest's view
of its virtual disk is not what the node's other tenants compete for.

Two details are what get this wrong, and both were measured rather than
assumed: the device has to be the whole disk, and the io controller has to
be enabled down the chain of parents or the files are not there at all.
"""

import os
import tempfile
from unittest import mock

from zun.container.cri import hostio
from zun.tests import base


class IoWeightTest(base.TestCase):
    """docker counts 10..1000, cgroup v2 counts 1..10000."""

    def test_the_ends_of_the_range_map_to_the_ends(self):
        self.assertEqual(1, hostio.io_weight(10))
        self.assertEqual(10000, hostio.io_weight(1000))

    def test_the_docker_default_lands_mid_range(self):
        self.assertEqual(4950, hostio.io_weight(500))

    def test_nothing_asked_for_is_not_a_weight(self):
        self.assertIsNone(hostio.io_weight(None))
        self.assertIsNone(hostio.io_weight(0))


class IoMaxLineTest(base.TestCase):

    def _container(self, **caps):
        fields = {field: None for _key, field in hostio.IO_MAX_FIELDS}
        fields.update(caps)
        return mock.Mock(**fields)

    def test_one_cap_leaves_the_others_unlimited(self):
        line = hostio.io_max_line('253:0',
                                  self._container(device_read_bps=1048576))

        self.assertEqual('253:0 rbps=1048576 wbps=max riops=max wiops=max',
                         line)

    def test_every_cap_at_once(self):
        line = hostio.io_max_line('253:0', self._container(
            device_read_bps=1, device_write_bps=2,
            device_read_iops=3, device_write_iops=4))

        self.assertEqual('253:0 rbps=1 wbps=2 riops=3 wiops=4', line)

    def test_no_cap_asked_for_is_no_line(self):
        """Writing an all-max line would still be a write nobody asked for."""
        self.assertIsNone(hostio.io_max_line('253:0', self._container()))

    def test_the_reset_line_takes_every_cap_off(self):
        self.assertEqual('253:0 rbps=max wbps=max riops=max wiops=max',
                         hostio.io_max_reset('253:0'))


class EnableIoControllerTest(base.TestCase):
    """A cgroup has a controller's files only if its parent enabled it."""

    def setUp(self):
        super(EnableIoControllerTest, self).setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(self._clean)

    def _clean(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _level(self, relative, enabled='cpuset cpu'):
        path = os.path.join(self.root, relative) if relative else self.root
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, 'cgroup.subtree_control'), 'w') as h:
            h.write(enabled)
        return path

    def _enabled(self, relative):
        path = os.path.join(self.root, relative) if relative else self.root
        with open(os.path.join(path, 'cgroup.subtree_control')) as h:
            return h.read()

    def test_every_level_above_the_leaf_is_enabled(self):
        self._level('')
        self._level('zun.slice')
        self._level('zun.slice/uuid')
        self._level('zun.slice/uuid/kata_sandbox')

        changed = hostio.enable_io_controller(
            '/zun.slice/uuid/kata_sandbox', root=self.root)

        self.assertEqual(3, len(changed))
        self.assertIn('+io', self._enabled('zun.slice'))
        self.assertIn('+io', self._enabled('zun.slice/uuid'))

    def test_the_leaf_itself_is_left_alone(self):
        """Its own setting governs its children, of which it has none."""
        self._level('')
        self._level('zun.slice')
        self._level('zun.slice/uuid')
        self._level('zun.slice/uuid/kata_sandbox')

        hostio.enable_io_controller('/zun.slice/uuid/kata_sandbox',
                                    root=self.root)

        self.assertEqual('cpuset cpu', self._enabled(
            'zun.slice/uuid/kata_sandbox'))

    def test_a_level_that_already_has_it_is_not_written_again(self):
        self._level('')
        self._level('zun.slice', enabled='cpuset cpu io')
        self._level('zun.slice/uuid')
        self._level('zun.slice/uuid/kata_sandbox')

        changed = hostio.enable_io_controller(
            '/zun.slice/uuid/kata_sandbox', root=self.root)

        self.assertNotIn(os.path.join(self.root, 'zun.slice'), changed)

    def test_a_level_that_cannot_be_prepared_is_raised(self):
        """The leaf would have no io files, which is a refusal to make."""
        self._level('')

        self.assertRaises(OSError, hostio.enable_io_controller,
                          '/zun.slice/uuid/kata_sandbox', root=self.root)
