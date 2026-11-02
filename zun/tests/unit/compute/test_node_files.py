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

"""Files of containers that are gone go; nothing else does."""

import os
import shutil
import tempfile
import time
from unittest import mock

from zun.compute import manager
from zun.compute import node_files
import zun.conf
from zun.tests import base

CONF = zun.conf.CONF
GONE = '11111111-1111-4111-8111-111111111111'
HERE = '22222222-2222-4222-8222-222222222222'
MEMBER = '33333333-3333-4333-8333-333333333333'
INIT = '44444444-4444-4444-8444-444444444444'
LONG_AGO = 7200


class Files(base.TestCase):

    def setUp(self):
        super(Files, self).setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def _file(self, name, age=LONG_AGO, root=None):
        path = os.path.join(root or self.root, name)
        open(path, 'w').close()
        then = time.time() - age
        os.utime(path, (then, then))
        return path


class UnclaimedTest(Files):

    def test_only_a_name_it_knows_and_no_claim_accounts_for(self):
        gone = self._file(GONE)
        self._file(HERE)
        self._file('oslo_read_shm_node_zun-compute')
        self._file(GONE + '.log')

        self.assertEqual({gone}, node_files.unclaimed(
            self.root, node_files.LOCK, {HERE}, 3600))

    def test_every_file_of_a_log(self):
        current = self._file(GONE + '.log')
        rotated = self._file(GONE + '.log.1')
        self._file(GONE + '.log.bak')

        self.assertEqual({current, rotated}, node_files.unclaimed(
            self.root, node_files.LOG_FILE, set(), 3600))

    def test_something_still_written_to_is_left(self):
        self._file(GONE, age=60)

        self.assertEqual(set(), node_files.unclaimed(
            self.root, node_files.LOCK, set(), 3600))

    def test_a_directory_with_the_name_is_left(self):
        os.mkdir(os.path.join(self.root, GONE))

        self.assertEqual(set(), node_files.unclaimed(
            self.root, node_files.LOCK, set(), 3600))

    def test_a_directory_that_is_not_there_has_nothing(self):
        self.assertEqual(set(), node_files.unclaimed(
            os.path.join(self.root, 'nope'), node_files.LOCK, set(), 3600))


class SweepTest(Files):

    def setUp(self):
        super(SweepTest, self).setUp()
        self.logs = tempfile.mkdtemp()
        self.hosts = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.logs)
        self.addCleanup(shutil.rmtree, self.hosts)
        p = mock.patch.object(manager.lockutils, 'get_lock_path',
                              return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        CONF.set_override('cri_log_root', self.logs)
        CONF.set_override('cri_hosts_root', self.hosts)
        self.mgr = manager.Manager.__new__(manager.Manager)
        self.mgr.host = 'node-1'
        self.mgr._unclaimed_files = set()
        capsule = mock.Mock(uuid=HERE, containers=[mock.Mock(uuid=MEMBER)],
                            init_containers=[mock.Mock(uuid=INIT)])
        for name, found in (('Capsule', [capsule]), ('Container', [])):
            p = mock.patch.object(getattr(manager.objects, name),
                                  'list_by_host', return_value=found)
            setattr(self, name.lower() + '_list', p.start())
            self.addCleanup(p.stop)

    def _sweep(self):
        self.mgr.reclaim_orphan_node_files(mock.Mock())

    def test_it_takes_two_sweeps(self):
        """A CNI daemon can still be using a lock just after the record went.
        """
        lock = self._file(GONE)

        self._sweep()
        self.assertTrue(os.path.exists(lock))

        self._sweep()
        self.assertFalse(os.path.exists(lock))

    def test_all_three_kinds_go(self):
        paths = [self._file(GONE),
                 self._file(GONE + '.log', root=self.logs),
                 self._file(GONE + '.log.1', root=self.logs),
                 self._file(GONE + '.hosts', root=self.hosts)]

        self._sweep()
        self._sweep()

        self.assertEqual([False] * 4, [os.path.exists(p) for p in paths])

    def test_a_capsule_and_its_members_keep_theirs(self):
        paths = [self._file(HERE),
                 self._file(HERE + '.hosts', root=self.hosts),
                 self._file(MEMBER + '.log', root=self.logs),
                 self._file(INIT + '.log.1', root=self.logs)]

        self._sweep()
        self._sweep()

        self.assertEqual([True] * 4, [os.path.exists(p) for p in paths])

    def test_one_that_came_back_between_sweeps_is_kept(self):
        lock = self._file(GONE)
        self._sweep()

        self.container_list.return_value = [mock.Mock(uuid=GONE)]
        self._sweep()
        self.container_list.return_value = []
        self._sweep()

        self.assertTrue(os.path.exists(lock))

    def test_an_unreadable_database_removes_nothing(self):
        """It reads as nothing being claimed."""
        lock = self._file(HERE)
        self.capsule_list.side_effect = RuntimeError('no database')

        self._sweep()
        self._sweep()

        self.assertTrue(os.path.exists(lock))
