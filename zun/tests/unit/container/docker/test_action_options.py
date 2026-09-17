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

"""API 1.54 on the docker driver: stop signal, exec -d, cp options, commit."""

import contextlib
from unittest import mock

from zun.container.docker import driver
from zun.tests import base


class TestDockerActionOptions(base.TestCase):

    def setUp(self):
        super(TestDockerActionOptions, self).setUp()
        self.docker = mock.Mock(timeout=60)
        self.docker._url.side_effect = lambda fmt, *a: fmt.format(*a)
        client = contextlib.contextmanager(lambda: (yield self.docker))
        self._patch = mock.patch.object(driver.docker_utils, 'docker_client',
                                        client)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.drv = driver.DockerDriver.__new__(driver.DockerDriver)
        self.container = mock.Mock(container_id='cid', uuid='u')

    def test_a_stop_signal_goes_on_the_stop_request(self):
        with mock.patch.object(driver.zun_network, 'driver'):
            self.drv.stop(mock.Mock(), self.container, 7, signal='SIGINT')

        self.docker.stop.assert_not_called()
        url, = self.docker._post.call_args[0]
        self.assertEqual('/containers/cid/stop', url)
        kwargs = self.docker._post.call_args.kwargs
        self.assertEqual({'signal': 'SIGINT', 't': 7}, kwargs['params'])
        self.assertGreater(kwargs['timeout'], 7)
        self.docker._raise_for_status.assert_called_once()

    def test_a_stop_without_one_is_as_before(self):
        with mock.patch.object(driver.zun_network, 'driver'):
            self.drv.stop(mock.Mock(), self.container, 7)

        self.docker.stop.assert_called_once_with('cid', timeout=7)
        self.docker._post.assert_not_called()

    def test_exec_detached_does_not_wait(self):
        self.drv.execute_detached('e1')

        self.docker.exec_start.assert_called_once_with('e1', detach=True)

    def test_put_archive_options_reach_the_daemon(self):
        self.drv.put_archive(mock.Mock(), self.container, '/x', b'tar',
                             copy_uidgid=True, no_overwrite_dir_non_dir=True)

        self.docker.put_archive.assert_not_called()
        self.assertEqual({'path': '/x', 'copyUIDGID': '1',
                          'noOverwriteDirNonDir': '1'},
                         self.docker._put.call_args.kwargs['params'])
        self.assertEqual(b'tar', self.docker._put.call_args.kwargs['data'])

    def test_put_archive_without_options_is_as_before(self):
        self.drv.put_archive(mock.Mock(), self.container, '/x', b'tar')

        self.docker.put_archive.assert_called_once_with('cid', '/x', b'tar')

    def test_commit_options_and_no_second_pause(self):
        self.drv.commit(mock.Mock(), self.container, 'r/app', '1',
                        message='m', author='a', changes=['ENV A=1'])

        self.docker.commit.assert_called_once_with(
            'cid', 'r/app', '1', message='m', author='a',
            changes=['ENV A=1'], pause=False)

    def test_commit_told_not_to_pause_does_not(self):
        self.drv.commit(mock.Mock(), self.container, 'r/app', '1',
                        pause=False)

        self.docker.commit.assert_called_once_with('cid', 'r/app', '1',
                                                   pause=False)

    def test_commit_without_options_is_as_before(self):
        self.drv.commit(mock.Mock(), self.container, 'r/app', '1')

        self.docker.commit.assert_called_once_with('cid', 'r/app', '1')
