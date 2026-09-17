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

"""API 1.53: docker's create options, accepted, stored and served."""

import json
from unittest import mock

from zun.tests.unit.api import base as api_base


_OPTIONS = {
    'extra_hosts': ['db:10.0.0.5', 'v6:2001:db8::1'],
    'dns_options': ['ndots:2', 'edns0'],
    'ulimits': [{'name': 'nofile', 'soft': 1024, 'hard': 4096}],
    'shm_size': 256,
    'read_only': 'true',
    'init': True,
    'group_add': ['audio', '1001'],
    'oom_score_adj': -500,
    'tmpfs': {'/run': 'rw,size=64m', '/tmp': ''},
    'healthcheck': {'cmd': 'true', 'interval': 5, 'start_period': 30},
}


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
    def test_every_option_is_stored_and_served(self, create):
        create.side_effect = lambda ctx, container, **kw: container
        self.assertEqual(202, self._create(**_OPTIONS).status_int)

        container = create.call_args[0][1]
        self.assertEqual(_OPTIONS['extra_hosts'], container.extra_hosts)
        self.assertEqual(_OPTIONS['dns_options'], container.dns_options)
        self.assertEqual(_OPTIONS['ulimits'], container.ulimits)
        self.assertEqual(256, container.shm_size)
        self.assertIs(True, container.read_only)
        self.assertIs(True, container.init)
        self.assertEqual(['audio', '1001'], container.group_add)
        self.assertEqual(-500, container.oom_score_adj)
        self.assertEqual(_OPTIONS['tmpfs'], container.tmpfs)
        self.assertEqual({'test': 'true', 'interval': 5,
                          'start_period': 30}, container.healthcheck)

        served = self.get('/v1/containers/').json['containers'][0]
        for key in ('extra_hosts', 'dns_options', 'ulimits', 'shm_size',
                    'group_add', 'oom_score_adj', 'tmpfs'):
            self.assertEqual(getattr(container, key), served[key], key)
        self.assertIs(True, served['read_only'])
        self.assertIs(True, served['init'])

    @mock.patch('zun.compute.api.API.container_create')
    def test_disabling_the_image_healthcheck_is_stored(self, create):
        create.side_effect = lambda ctx, container, **kw: container
        self.assertEqual(202, self._create(
            healthcheck={'disable': True}).status_int)

        self.assertTrue(create.call_args[0][1].healthcheck['disable'])

    @mock.patch('zun.compute.api.API.container_create')
    def test_invalid_values_are_refused_before_anything_is_made(self,
                                                                create):
        for field, value in (
                ('extra_hosts', ['no-address']),
                ('extra_hosts', ['a:b'] * 65),
                ('dns_options', ['bad option']),
                ('ulimits', [{'name': 'nofile', 'soft': 1}]),
                ('ulimits', [{'name': 'bogus', 'soft': 1, 'hard': 1}]),
                ('ulimits', [{'name': 'nofile', 'soft': 1, 'hard': 1,
                              'extra': 1}]),
                ('shm_size', 0),
                ('shm_size', 'big'),
                ('read_only', 'maybe'),
                ('group_add', ['has space']),
                ('oom_score_adj', 1001),
                ('oom_score_adj', -1001),
                ('tmpfs', {'relative': ''}),
                ('tmpfs', {'/run': 'size=1m;rm -rf'}),
                ('healthcheck', {'cmd': 'true', 'interval': 'soon'}),
                ('healthcheck', {'cmd': 'true', 'start_period': -1}),
                ('healthcheck', {'disable': True, 'cmd': 'true'})):
            response = self._create(expect_errors=True, **{field: value})
            self.assertEqual(400, response.status_int, (field, value))
        create.assert_not_called()
