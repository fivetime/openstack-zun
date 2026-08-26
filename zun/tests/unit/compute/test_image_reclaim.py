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

"""Removing images nothing on a node is using.

A container gets a quota for what it writes; the layers underneath it get
none, and nothing removed them, so a node filled at the pace its tenants
pulled -- and failed for every tenant on it, including the ones who had
pulled nothing.

These tests are mostly about what must *not* be removed.
"""

from unittest import mock

from zun.compute import manager
from zun.tests import base


def image(image_id, tags=None, size=100, pinned=False):
    return {'id': image_id, 'tags': tags or [], 'size': size,
            'pinned': pinned}


class ReclaimUnusedImagesTest(base.TestCase):

    def setUp(self):
        super(ReclaimUnusedImagesTest, self).setUp()
        self.config(reclaim_unused_images=True,
                    reclaim_unused_images_interval=0,
                    reclaim_unused_images_sweeps=2,
                    group='compute')
        self.manager = manager.Manager.__new__(manager.Manager)
        self.manager._last_report = {}
        self.manager._unused_images = {}
        self.manager.driver = mock.Mock()

    def _sweep(self, images, in_use):
        self.manager.driver.list_local_images.return_value = images
        self.manager.driver.images_in_use.return_value = set(in_use)
        self.manager.reclaim_unused_images({})

    def _removed(self):
        return [c.args[0]
                for c in self.manager.driver.remove_local_image.call_args_list]

    def test_an_image_in_use_is_never_removed(self):
        for _ in range(5):
            self._sweep([image('busy')], in_use=['busy'])

        self.assertEqual([], self._removed())

    def test_a_pinned_image_is_never_removed(self):
        """The runtime says it needs this one; that is not ours to argue."""
        for _ in range(5):
            self._sweep([image('pinned-one', pinned=True)], in_use=[])

        self.assertEqual([], self._removed())

    def test_an_unused_image_survives_the_first_sweep(self):
        """It may belong to a container that does not exist yet."""
        self._sweep([image('fresh')], in_use=[])

        self.assertEqual([], self._removed())

    def test_an_unused_image_goes_after_enough_sweeps(self):
        self._sweep([image('cold')], in_use=[])
        self._sweep([image('cold')], in_use=[])

        self.assertEqual(['cold'], self._removed())

    def test_becoming_used_again_starts_the_count_over(self):
        """A pull followed by a create must not be caught mid-way."""
        self._sweep([image('warming')], in_use=[])
        self._sweep([image('warming')], in_use=['warming'])
        self._sweep([image('warming')], in_use=[])

        self.assertEqual([], self._removed())

    def test_an_image_another_runtime_user_holds_is_kept(self):
        """in_use comes from the runtime, so this is somebody else's.

        The listing loses what was removed, as a real runtime's would, so
        that the sweep after a removal is the one a node actually sees.
        """
        held = [image('theirs'), image('ours')]

        def forget(image_id):
            held[:] = [i for i in held if i['id'] != image_id]
        self.manager.driver.remove_local_image.side_effect = forget

        for _ in range(4):
            self._sweep(list(held), in_use=['theirs'])

        self.assertEqual(['ours'], self._removed())

    def test_nothing_happens_when_it_is_turned_off(self):
        self.config(reclaim_unused_images=False, group='compute')
        for _ in range(4):
            self._sweep([image('cold')], in_use=[])

        self.assertEqual([], self._removed())

    def test_a_node_that_cannot_list_removes_nothing(self):
        self.manager.driver.list_local_images.side_effect = RuntimeError('x')
        self.manager.reclaim_unused_images({})

        self.assertEqual([], self._removed())

    def test_a_failed_removal_is_left_for_the_next_sweep(self):
        self.manager.driver.remove_local_image.side_effect = RuntimeError(
            'in use')
        self._sweep([image('stubborn')], in_use=[])
        self._sweep([image('stubborn')], in_use=[])

        self.assertEqual(['stubborn'], self._removed())
        self.assertIn('stubborn', self.manager._unused_images)

    def test_an_image_that_disappeared_is_forgotten(self):
        self._sweep([image('gone')], in_use=[])
        self._sweep([], in_use=[])

        self.assertEqual({}, self.manager._unused_images)
