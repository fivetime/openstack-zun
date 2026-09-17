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

"""API 1.54 on the CRI driver: carried where it can be, refused elsewhere."""

from unittest import mock

import grpc

from zun.common import exception
from zun.container.cri import commit as cri_commit
from zun.container.cri import driver as cri_driver
from zun.criapi import api_pb2
from zun.tests import base


class TestApplyChanges(base.TestCase):

    def test_each_instruction_lands_in_the_config(self):
        config = {'Env': ['PATH=/bin', 'A=old'], 'Labels': {'k': 'v'}}
        cri_commit.apply_changes(config, [
            'CMD ["nginx", "-g", "daemon off;"]',
            'ENTRYPOINT /docker-entrypoint.sh',
            'ENV A=new B="two words"',
            'ENV C legacy form',
            'LABEL x=y "z"=1',
            'EXPOSE 80 53/udp',
            'VOLUME ["/data"]',
            'VOLUME /a /b',
            'STOPSIGNAL SIGQUIT',
            'USER 1000:1000',
            'WORKDIR /srv'])

        self.assertEqual(['nginx', '-g', 'daemon off;'], config['Cmd'])
        self.assertEqual(['/bin/sh', '-c', '/docker-entrypoint.sh'],
                         config['Entrypoint'])
        self.assertEqual(['PATH=/bin', 'A=new', 'B=two words',
                          'C=legacy form'], config['Env'])
        self.assertEqual({'k': 'v', 'x': 'y', 'z': '1'}, config['Labels'])
        self.assertEqual({'80/tcp': {}, '53/udp': {}},
                         config['ExposedPorts'])
        self.assertEqual({'/data': {}, '/a': {}, '/b': {}},
                         config['Volumes'])
        self.assertEqual('SIGQUIT', config['StopSignal'])
        self.assertEqual('1000:1000', config['User'])
        self.assertEqual('/srv', config['WorkingDir'])

    def test_an_instruction_commit_does_not_apply_is_refused(self):
        for bad in ('RUN true', 'LABEL novalue', 'ONBUILD RUN x'):
            self.assertRaises(ValueError, cri_commit.apply_changes, {},
                              [bad])


class TestCriActionOptions(base.TestCase):

    def setUp(self):
        super(TestCriActionOptions, self).setUp()
        self.drv = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.drv.runtime_stub = mock.Mock()
        self.drv.task_stub = mock.Mock()
        self.container = mock.Mock(container_id='cid', uuid='u')

    def test_a_stop_signal_is_sent_first_then_the_stop(self):
        status = mock.Mock()
        status.status.state = api_pb2.CONTAINER_EXITED
        self.drv.runtime_stub.ContainerStatus.return_value = status

        self.drv.stop(mock.Mock(), self.container, 9, signal='SIGINT')

        kill = self.drv.task_stub.Kill.call_args[0][0]
        self.assertEqual(2, kill.signal)
        stop = self.drv.runtime_stub.StopContainer.call_args[0][0]
        self.assertEqual(0, stop.timeout)

    def test_a_process_that_ignores_it_is_stopped_after_the_grace(self):
        status = mock.Mock()
        status.status.state = api_pb2.CONTAINER_RUNNING
        self.drv.runtime_stub.ContainerStatus.return_value = status
        with mock.patch.object(cri_driver.time, 'sleep'), \
                mock.patch.object(cri_driver.time, 'time',
                                  side_effect=[0, 1, 5]):
            self.drv.stop(mock.Mock(), self.container, 2, signal='SIGINT')

        stop = self.drv.runtime_stub.StopContainer.call_args[0][0]
        self.assertEqual(2, stop.timeout)

    def test_a_vanished_container_counts_as_exited(self):
        self.drv.runtime_stub.ContainerStatus.side_effect = grpc.RpcError()

        self.assertTrue(self.drv._exited_within(self.container, 5))

    def test_without_a_signal_the_stop_is_as_before(self):
        self.drv.stop(mock.Mock(), self.container, 9)

        self.drv.task_stub.Kill.assert_not_called()
        self.assertEqual(
            9, self.drv.runtime_stub.StopContainer.call_args[0][0].timeout)

    def test_exec_detached_is_refused(self):
        self.assertRaises(exception.Invalid, self.drv.execute_detached, 'e')

    def test_put_archive_options_are_refused_before_anything_runs(self):
        with mock.patch.object(self.drv, '_run_or_raise') as run:
            for options in ({'copy_uidgid': True},
                            {'no_overwrite_dir_non_dir': True}):
                self.assertRaises(exception.Invalid, self.drv.put_archive,
                                  mock.Mock(), self.container, '/x', b't',
                                  **options)
        run.assert_not_called()

    def test_commit_passes_its_options_to_the_helper(self):
        with mock.patch.object(self.drv, '_run_commit_cli') as cli:
            self.drv.commit(mock.Mock(), mock.Mock(
                container_id='cid', uuid='u', image='alpine'), 'r/app', '1',
                message='m', author='a', changes=['ENV A=1'], pause=False)

        request = cli.call_args[0][0]
        self.assertEqual(('m', 'a', ['ENV A=1']),
                         (request['message'], request['author'],
                          request['changes']))

    def test_commit_refuses_a_bad_change_itself(self):
        with mock.patch.object(self.drv, '_run_commit_cli') as cli:
            self.assertRaises(exception.Invalid, self.drv.commit,
                              mock.Mock(), self.container, 'r', '1',
                              changes=['RUN rm -rf /'])
        cli.assert_not_called()

    def test_a_variable_without_a_value_is_refused_at_create(self):
        container = mock.Mock(extra_hosts=None, ulimits=None, shm_size=None,
                              init=None, tmpfs=None, group_add=None,
                              environment={'A': '1', 'B': None, 'C': None})
        error = self.assertRaises(
            exception.Invalid, cri_driver._refuse_what_cri_cannot_carry,
            container)

        self.assertIn('B, C', str(error))
