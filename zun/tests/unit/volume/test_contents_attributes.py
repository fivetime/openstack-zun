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

"""A contents file's mode and owner are set on the node (API 1.54)."""

import os
import stat
from unittest import mock

import fixtures

from zun.tests import base
from zun.volume import driver


class TestContentsAttributes(base.TestCase):

    def setUp(self):
        super(TestContentsAttributes, self).setUp()
        self.path = os.path.join(self.useFixture(fixtures.TempDir()).path,
                                 'f')
        with open(self.path, 'w') as f:
            f.write('x')
        os.chmod(self.path, 0o644)

    def _volmap(self, **fields):
        volmap = mock.Mock(spec=['file_mode', 'file_uid', 'file_gid'])
        volmap.file_mode = fields.get('mode')
        volmap.file_uid = fields.get('uid')
        volmap.file_gid = fields.get('gid')
        return volmap

    def test_the_mode_is_set(self):
        driver._apply_file_attributes(self.path, self._volmap(mode=0o400))

        self.assertEqual(0o400, stat.S_IMODE(os.stat(self.path).st_mode))

    def test_the_owner_is_set_with_the_other_half_left_alone(self):
        with mock.patch.object(driver.os, 'chown') as chown:
            driver._apply_file_attributes(self.path, self._volmap(uid=1000))
            driver._apply_file_attributes(self.path, self._volmap(gid=7))

        self.assertEqual([mock.call(self.path, 1000, -1),
                          mock.call(self.path, -1, 7)],
                         chown.call_args_list)

    def test_nothing_asked_changes_nothing(self):
        with mock.patch.object(driver.os, 'chown') as chown:
            driver._apply_file_attributes(self.path, self._volmap())
            driver._apply_file_attributes(self.path, mock.Mock())

        chown.assert_not_called()
        self.assertEqual(0o644, stat.S_IMODE(os.stat(self.path).st_mode))
