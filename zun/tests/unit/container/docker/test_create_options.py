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

"""The API 1.53 create options reach docker as docker spells them."""

import os
from unittest import mock

from docker import types as docker_types
import fixtures

from zun.container.docker import driver
from zun.tests import base


def _container(**fields):
    defaults = dict(extra_hosts=None, ulimits=None, shm_size=None,
                    read_only=None, init=None, group_add=None,
                    oom_score_adj=None, tmpfs=None, dns_options=None,
                    dns=None, dns_search=None, healthcheck=None,
                    uuid='c-1')
    defaults.update(fields)
    return mock.Mock(**defaults)


class TestCreateOptions(base.TestCase):

    def _host_config(self, container, start=None):
        host_config = dict(start or {})
        drv = driver.DockerDriver.__new__(driver.DockerDriver)
        drv._apply_create_options(container, host_config)
        return host_config

    def test_nothing_asked_changes_nothing(self):
        self.assertEqual({}, self._host_config(_container()))

    def test_each_option_lands_on_its_host_config_key(self):
        host_config = self._host_config(_container(
            extra_hosts=['db:10.0.0.5', 'v6:2001:db8::1'],
            ulimits=[{'name': 'nofile', 'soft': 1024, 'hard': 4096}],
            shm_size=256, read_only=True, init=True,
            group_add=['audio', '1001'], oom_score_adj=-500,
            tmpfs={'/run': 'rw,size=64m'}))

        self.assertEqual(['db:10.0.0.5', 'v6:2001:db8::1'],
                         host_config['extra_hosts'])
        self.assertEqual([docker_types.Ulimit(name='nofile', soft=1024,
                                              hard=4096)],
                         host_config['ulimits'])
        self.assertEqual(256 * 1024 * 1024, host_config['shm_size'])
        self.assertTrue(host_config['read_only'])
        self.assertTrue(host_config['init'])
        self.assertEqual(['audio', '1001'], host_config['group_add'])
        self.assertEqual(-500, host_config['oom_score_adj'])
        self.assertEqual({'/run': 'rw,size=64m'}, host_config['tmpfs'])

    def test_they_are_what_docker_py_builds_a_host_config_from(self):
        host_config = self._host_config(_container(
            extra_hosts=['db:10.0.0.5'],
            ulimits=[{'name': 'nofile', 'soft': 1, 'hard': 2}],
            shm_size=64, read_only=True, init=False, group_add=['1'],
            oom_score_adj=10, tmpfs={'/run': ''}))

        built = docker_types.HostConfig(version='1.44', **host_config)

        self.assertEqual(['db:10.0.0.5'], built['ExtraHosts'])
        self.assertEqual(64 * 1024 * 1024, built['ShmSize'])
        self.assertTrue(built['ReadonlyRootfs'])
        self.assertFalse(built['Init'])
        self.assertEqual(['1'], built['GroupAdd'])
        self.assertEqual(10, built['OomScoreAdj'])
        self.assertEqual({'/run': ''}, built['Tmpfs'])
        self.assertEqual([{'Name': 'nofile', 'Soft': 1, 'Hard': 2}],
                         built['Ulimits'])

    def test_init_false_is_said_rather_than_left_to_the_daemon(self):
        self.assertFalse(self._host_config(_container(init=False))['init'])

    def test_groups_follow_those_the_security_context_added(self):
        host_config = self._host_config(_container(group_add=['audio']),
                                        start={'group_add': ['2000']})

        self.assertEqual(['2000', 'audio'], host_config['group_add'])

    def test_dns_options_go_to_docker_without_a_resolv_conf_of_ours(self):
        self.assertEqual(['ndots:2'], self._host_config(
            _container(dns_options=['ndots:2']))['dns_opt'])

    def test_dns_options_go_into_our_resolv_conf_when_there_is_one(self):
        self.assertNotIn('dns_opt', self._host_config(
            _container(dns=['10.0.0.2'], dns_options=['ndots:2'])))
        state = self.useFixture(fixtures.TempDir()).path
        self.config(state_path=state)
        drv = driver.DockerDriver.__new__(driver.DockerDriver)

        path = drv._write_resolv_conf(_container(
            dns=['10.0.0.2'], dns_options=['ndots:2', 'timeout:1']))

        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual('options ndots:2 timeout:1', lines[-1])
        self.assertIn('options ndots:1', lines)
        self.assertTrue(os.path.isfile(path))


class TestHealthcheck(base.TestCase):

    def test_seconds_become_nanoseconds_start_period_included(self):
        spec = driver._healthcheck(_container(healthcheck={
            'test': 'true', 'interval': 5, 'timeout': 2, 'retries': 3,
            'start_period': 30}))

        self.assertEqual({'test': 'true', 'interval': 5 * 10 ** 9,
                          'timeout': 2 * 10 ** 9, 'retries': 3,
                          'start_period': 30 * 10 ** 9}, spec)
        self.assertEqual(30 * 10 ** 9,
                         docker_types.Healthcheck(**spec)['StartPeriod'])

    def test_disable_is_docker_none(self):
        spec = driver._healthcheck(_container(healthcheck={'disable': True,
                                                           'test': ''}))

        self.assertEqual({'test': ['NONE']}, spec)
        self.assertEqual(['NONE'], docker_types.Healthcheck(**spec)['Test'])

    def test_a_capsule_members_probes_alone_are_not_a_check(self):
        self.assertIsNone(driver._healthcheck(_container(healthcheck={
            'k8s_probes': {'liveness': {}}})))
        self.assertIsNone(driver._healthcheck(_container(healthcheck=None)))

    def test_without_a_start_period_none_is_sent(self):
        spec = driver._healthcheck(_container(healthcheck={
            'test': 'true', 'interval': 1}))

        self.assertNotIn('start_period', spec)
