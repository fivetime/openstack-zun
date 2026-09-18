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

"""The image's ENTRYPOINT and CMD are filled in the way docker does."""

from unittest import mock

from zun.container.docker import driver
from zun.tests import base


IMAGE = {'Config': {'Entrypoint': ['/docker-entrypoint.sh'],
                    'Cmd': ['nginx', '-g', 'daemon off;']}}


class MergeImageCommandTest(base.BaseTestCase):

    def _merge(self, entrypoint, command, image=IMAGE):
        docker = mock.Mock()
        docker.inspect_image.return_value = image
        container = mock.Mock(entrypoint=entrypoint, command=command)
        driver._merge_image_command(docker, 'nginx:alpine', container)
        return container, docker

    def test_nothing_given_takes_both_from_the_image(self):
        container, _ = self._merge(None, None)
        self.assertEqual(['/docker-entrypoint.sh'], container.entrypoint)
        self.assertEqual(['nginx', '-g', 'daemon off;'], container.command)

    def test_a_command_alone_keeps_the_images_entrypoint(self):
        container, _ = self._merge(None, ['nginx', '-T'])
        self.assertEqual(['/docker-entrypoint.sh'], container.entrypoint)
        self.assertEqual(['nginx', '-T'], container.command)

    def test_an_entrypoint_alone_runs_without_the_images_cmd(self):
        """`docker run --entrypoint echo nginx` prints an empty line."""
        container, docker = self._merge(['echo'], None)
        self.assertEqual(['echo'], container.entrypoint)
        self.assertIsNone(container.command)
        docker.inspect_image.assert_not_called()

    def test_an_entrypoint_with_an_empty_command_stays_empty(self):
        container, _ = self._merge(['echo'], [])
        self.assertEqual([], container.command)

    def test_both_given_are_used_as_given(self):
        container, docker = self._merge(['sh'], ['-c', 'true'])
        self.assertEqual(['sh'], container.entrypoint)
        self.assertEqual(['-c', 'true'], container.command)
        docker.inspect_image.assert_not_called()

    def test_an_image_without_either_leaves_them_unset(self):
        container, _ = self._merge(None, None, image={'Config': {}})
        self.assertIsNone(container.entrypoint)
        self.assertIsNone(container.command)
