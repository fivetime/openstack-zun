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

"""The API 1.53 create options on the CRI driver: carried or refused."""

from unittest import mock

from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


def _container(**fields):
    defaults = dict(extra_hosts=None, ulimits=None, shm_size=None,
                    read_only=None, init=None, group_add=None,
                    oom_score_adj=None, tmpfs=None, dns_options=None,
                    user=None, privileged=False, healthcheck=None)
    defaults.update(fields)
    return mock.Mock(**defaults)


class TestCarried(base.TestCase):

    def test_read_only_and_numeric_groups_reach_the_security_context(self):
        sc = cri_driver._linux_security_context(
            _container(read_only=True, group_add=['1001', '1002']))

        self.assertTrue(sc.readonly_rootfs)
        self.assertEqual([1001, 1002], list(sc.supplemental_groups))

    def test_fs_group_is_added_to_the_groups_asked_for(self):
        sc = cri_driver._linux_security_context(_container(
            group_add=['1001'],
            healthcheck={'k8s_security_context': {'fsGroup': 2000}}))

        self.assertEqual([1001, 2000], list(sc.supplemental_groups))

    def test_nothing_asked_sets_nothing(self):
        sc = cri_driver._linux_security_context(_container())

        self.assertFalse(sc.readonly_rootfs)
        self.assertEqual([], list(sc.supplemental_groups))

    def test_dns_options_reach_the_sandbox(self):
        drv = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        capsule = mock.Mock(uuid='c-1', hostname=None)
        with mock.patch.object(drv, '_sandbox_cgroup_parent',
                               return_value=''), \
                mock.patch.object(drv, '_log_directory', return_value='/l'), \
                mock.patch.object(drv, '_member_resources',
                                  return_value=[]), \
                mock.patch.object(cri_driver.cri_resources,
                                  'sandbox_resources', return_value={}):
            config = drv._get_sandbox_config(
                capsule, ['10.0.0.2'], ['svc'], dns_options=['ndots:2'])

        self.assertEqual(['ndots:2'], list(config.dns_config.options))


class TestRefused(base.TestCase):

    def test_each_option_without_a_cri_field_is_refused_by_name(self):
        for field, value in (('extra_hosts', ['db:10.0.0.5']),
                             ('ulimits', [{'name': 'nofile', 'soft': 1,
                                           'hard': 2}]),
                             ('shm_size', 64),
                             ('init', True),
                             ('tmpfs', {'/run': ''})):
            error = self.assertRaises(
                exception.Invalid, cri_driver._refuse_what_cri_cannot_carry,
                _container(**{field: value}))
            self.assertIn(field, str(error))

    def test_a_named_group_is_refused_rather_than_dropped(self):
        error = self.assertRaises(
            exception.Invalid, cri_driver._refuse_what_cri_cannot_carry,
            _container(group_add=['audio']))

        self.assertIn('audio', str(error))

    def test_init_false_and_what_it_can_carry_pass(self):
        cri_driver._refuse_what_cri_cannot_carry(_container(
            init=False, read_only=True, group_add=['1'], oom_score_adj=5,
            dns_options=['ndots:2']))

    def test_the_refusal_comes_before_any_sandbox(self):
        drv = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        with mock.patch.object(drv, '_create_pod_sandbox') as sandbox:
            self.assertRaises(exception.Invalid, drv.create, mock.Mock(),
                              _container(shm_size=64, uuid='c-1'), {}, [],
                              {})
        sandbox.assert_not_called()
