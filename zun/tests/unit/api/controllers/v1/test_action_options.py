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

"""API 1.54: options on stop, execute, put_archive, commit and create."""

import base64
import json
from unittest import mock

from oslo_config import fixture as config_fixture

from zun.api.controllers.v1 import containers as controller
from zun import conf
from zun.tests.unit.api import base as api_base
from zun.tests.unit.db import utils


def _encoded(size):
    return base64.b64encode(b'x' * size).decode()


@mock.patch('zun.common.utils.validate_container_state', new=mock.Mock())
class TestActionOptions(api_base.FunctionalTest):

    def setUp(self):
        super(TestActionOptions, self).setUp()
        self.container = utils.create_test_container(context=self.context,
                                                     status='Running')
        self.base = '/v1/containers/%s' % self.container.uuid

    @mock.patch('zun.compute.api.API.container_stop')
    def test_stop_with_a_signal_passes_it_on(self, stop):
        response = self.post(self.base + '/stop?timeout=5&signal=SIGINT')

        self.assertEqual(202, response.status_int)
        self.assertEqual('SIGINT', stop.call_args.kwargs['signal'])

    @mock.patch('zun.compute.api.API.container_stop')
    def test_stop_without_one_sends_no_signal_argument(self, stop):
        self.post(self.base + '/stop?timeout=5')

        self.assertNotIn('signal', stop.call_args.kwargs)

    @mock.patch('zun.compute.api.API.container_stop')
    def test_an_unknown_signal_is_refused(self, stop):
        response = self.post(self.base + '/stop?signal=SIGNOPE',
                             expect_errors=True)

        self.assertEqual(400, response.status_int)
        stop.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec')
    def test_execute_detach(self, execute):
        execute.return_value = {'output': None, 'exit_code': None,
                                'exec_id': 'e1', 'proxy_url': None}
        response = self.post(self.base + '/execute?command=sleep+9&run=true'
                             '&detach=true')

        self.assertEqual(200, response.status_int)
        self.assertTrue(execute.call_args.kwargs['detach'])

    @mock.patch('zun.compute.api.API.container_exec')
    def test_detach_is_not_an_interactive_session(self, execute):
        for query in ('run=true&interactive=true&detach=true',
                      'run=false&detach=true'):
            response = self.post(self.base + '/execute?command=x&' + query,
                                 expect_errors=True)
            self.assertEqual(400, response.status_int, query)
        execute.assert_not_called()

    @mock.patch('zun.compute.api.API.container_exec')
    def test_execute_without_detach_is_as_before(self, execute):
        execute.return_value = {'output': 'hi', 'exit_code': 0,
                                'exec_id': None, 'proxy_url': None}
        self.post(self.base + '/execute?command=echo&run=true')

        self.assertNotIn('detach', execute.call_args.kwargs)

    @mock.patch('zun.compute.api.API.container_put_archive')
    def test_put_archive_options(self, put):
        self.post(self.base + '/put_archive?path=/x&copy_uidgid=true'
                  '&no_overwrite_dir_non_dir=true',
                  params=json.dumps({'data': 'dGFy'}),
                  content_type='application/json')

        self.assertEqual({'copy_uidgid': True,
                          'no_overwrite_dir_non_dir': True},
                         put.call_args.kwargs['options'])

    @mock.patch('zun.compute.api.API.container_put_archive')
    def test_put_archive_false_options_are_not_sent(self, put):
        self.post(self.base + '/put_archive?path=/x&copy_uidgid=false',
                  params=json.dumps({'data': 'dGFy'}),
                  content_type='application/json')

        self.assertNotIn('options', put.call_args.kwargs)

    @mock.patch('zun.compute.api.API.container_commit')
    def test_commit_options(self, commit):
        commit.return_value = {'uuid': 'i'}
        changes = 'ENV A=1\nCMD ["sh"]\nLABEL x="y z"'
        response = self.post(
            self.base + '/commit?repository=r.example.com/p/app&tag=1'
            '&message=hello&author=me&pause=false&changes=' +
            __import__('urllib.parse').parse.quote(changes))

        self.assertEqual(202, response.status_int)
        self.assertEqual({'message': 'hello', 'author': 'me',
                          'changes': ['ENV A=1', 'CMD ["sh"]',
                                      'LABEL x="y z"'],
                          'pause': False},
                         commit.call_args.kwargs['options'])

    @mock.patch('zun.compute.api.API.container_commit')
    def test_commit_refuses_an_instruction_commit_does_not_apply(self,
                                                                 commit):
        for bad in ('RUN rm -rf /', 'FROM x', 'ENV'):
            response = self.post(
                self.base + '/commit?repository=r&changes=' +
                __import__('urllib.parse').parse.quote(bad),
                expect_errors=True)
            self.assertEqual(400, response.status_int, bad)
        commit.assert_not_called()

    @mock.patch('zun.compute.api.API.container_commit')
    def test_commit_without_options_is_as_before(self, commit):
        commit.return_value = {'uuid': 'i'}
        self.post(self.base + '/commit?repository=r&pause=true')

        self.assertNotIn('options', commit.call_args.kwargs)


class TestCommitOptionsHelper(api_base.FunctionalTest):

    def test_blank_lines_are_not_instructions(self):
        self.assertEqual({'changes': ['ENV A=1']},
                         controller._commit_options(
                             {'changes': '\nENV A=1\n\n'}))


@mock.patch('zun.network.neutron.NeutronAPI.get_available_network',
            new=mock.Mock(return_value={'id': 'net'}))
@mock.patch('zun.compute.api.API.image_search', new=mock.Mock())
class TestCreateOptions(api_base.FunctionalTest):

    def _create(self, expect_errors=False, **fields):
        body = {'name': 'web', 'image': 'alpine'}
        body.update(fields)
        return self.post('/v1/containers/', params=json.dumps(body),
                         content_type='application/json',
                         expect_errors=expect_errors)

    @mock.patch('zun.compute.api.API.container_create')
    def test_a_variable_without_a_value_is_stored_as_null(self, create):
        create.side_effect = lambda ctx, container, **kw: container
        self.assertEqual(202, self._create(
            environment={'KEEP': 'x', 'DROP': None}).status_int)

        self.assertEqual({'KEEP': 'x', 'DROP': None},
                         create.call_args[0][1].environment)

    @mock.patch('zun.compute.api.API.container_create')
    def test_a_contents_file_carries_its_mode_and_owner(self, create):
        self.assertEqual(202, self._create(mounts=[
            {'type': 'bind', 'source': _encoded(10), 'destination': '/s',
             'mode': 0o400, 'uid': 1000, 'gid': 1000}]).status_int)

        volmap = create.call_args.kwargs['requested_volumes'][
            create.call_args[0][1].uuid][0]
        self.assertEqual((0o400, 1000, 1000),
                         (volmap.file_mode, volmap.file_uid,
                          volmap.file_gid))

    @mock.patch('zun.compute.api.API.container_create')
    def test_mode_on_a_volume_is_refused(self, create):
        with mock.patch('zun.volume.cinder_api.CinderAPI.create_volume'), \
                mock.patch('zun.volume.cinder_api.CinderAPI.'
                           'ensure_volume_usable'):
            response = self._create(expect_errors=True, mounts=[
                {'size': '1', 'destination': '/d', 'mode': 0o700}])

        self.assertEqual(400, response.status_int)
        create.assert_not_called()

    @mock.patch('zun.compute.api.API.container_create')
    def test_a_contents_file_over_the_limit_is_refused(self, create):
        self.useFixture(config_fixture.Config(conf.CONF)).config(
            max_contents_size=64, group='volume')

        response = self._create(expect_errors=True, mounts=[
            {'type': 'bind', 'source': _encoded(65), 'destination': '/s'}])

        self.assertEqual(400, response.status_int)
        self.assertIn('64', response.text)
        create.assert_not_called()
        self.assertEqual(202, self._create(mounts=[
            {'type': 'bind', 'source': _encoded(64),
             'destination': '/s'}]).status_int)

    @mock.patch('zun.compute.api.API.container_create')
    def test_mode_out_of_range_is_refused(self, create):
        for field, value in (('mode', 0o10000), ('uid', -1), ('gid', 'x')):
            response = self._create(expect_errors=True, mounts=[
                {'type': 'bind', 'source': _encoded(1),
                 'destination': '/s', field: value}])
            self.assertEqual(400, response.status_int, field)
