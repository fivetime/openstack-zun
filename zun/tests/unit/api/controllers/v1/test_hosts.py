# Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from unittest import mock
from unittest.mock import patch

from oslo_utils import uuidutils

from zun import objects
from zun.objects import numa
from zun.tests.unit.api import base as api_base
from zun.tests.unit.db import utils


class TestHostController(api_base.FunctionalTest):

    @mock.patch('zun.common.policy.enforce')
    @patch('zun.objects.ComputeNode.list')
    def test_get_all_hosts(self, mock_host_list, mock_policy):
        mock_policy.return_value = True
        test_host = utils.get_test_compute_node()
        numat = numa.NUMATopology._from_dict(test_host['numa_topology'])
        test_host['numa_topology'] = numat
        hosts = [objects.ComputeNode(self.context, **test_host)]
        mock_host_list.return_value = hosts

        response = self.get('/v1/hosts')

        mock_host_list.assert_called_once_with(mock.ANY,
                                               1000, None, 'hostname', 'asc',
                                               filters=None)
        self.assertEqual(200, response.status_int)
        actual_hosts = response.json['hosts']
        self.assertEqual(1, len(actual_hosts))
        self.assertEqual(test_host['uuid'],
                         actual_hosts[0].get('uuid'))

    @mock.patch('zun.common.policy.enforce')
    @patch('zun.objects.ComputeNode.list')
    def test_get_all_hosts_with_pagination_marker(self, mock_host_list,
                                                  mock_policy):
        mock_policy.return_value = True
        host_list = []
        for id_ in range(4):
            test_host = utils.create_test_compute_node(
                context=self.context,
                uuid=uuidutils.generate_uuid(),
                rp_uuid=uuidutils.generate_uuid())
            numat = numa.NUMATopology._from_dict(test_host['numa_topology'])
            test_host['numa_topology'] = numat
            host = objects.ComputeNode(self.context, **test_host)
            host_list.append(host)
        mock_host_list.return_value = host_list[-1:]
        response = self.get('/v1/hosts?limit=3&marker=%s'
                            % host_list[2].uuid)

        self.assertEqual(200, response.status_int)
        actual_hosts = response.json['hosts']
        self.assertEqual(1, len(actual_hosts))
        self.assertEqual(host_list[-1].uuid,
                         actual_hosts[0].get('uuid'))

    @mock.patch('zun.common.policy.enforce')
    @patch('zun.objects.ComputeNode.get_by_uuid')
    def test_get_one_host(self, mock_get_by_uuid, mock_policy):
        mock_policy.return_value = True
        test_host = utils.get_test_compute_node()
        numat = numa.NUMATopology._from_dict(test_host['numa_topology'])
        test_host['numa_topology'] = numat
        test_host_obj = objects.ComputeNode(self.context, **test_host)
        mock_get_by_uuid.return_value = test_host_obj
        response = self.get('/v1/hosts/%s' % test_host['uuid'])
        mock_get_by_uuid.assert_called_once_with(
            mock.ANY,
            test_host['uuid'])
        self.assertEqual(200, response.status_int)
        self.assertEqual(test_host['uuid'],
                         response.json['uuid'])


class TestHostEnforcement(api_base.FunctionalTest):

    def _common_policy_check(self, rule, func, *arg, **kwarg):
        self.policy.set_rules({rule: 'project_id:non_fake'})
        response = func(*arg, **kwarg)
        self.assertEqual(403, response.status_int)
        self.assertEqual('application/json', response.content_type)
        self.assertTrue(
            "Policy doesn't allow %s to be performed." % rule,
            response.json['errors'][0]['detail'])

    def test_policy_disallow_get_all(self):
        self._common_policy_check(
            'host:get_all', self.get_json, '/hosts',
            expect_errors=True)

    def test_pollicy_disallow_get_one(self):
        self._common_policy_check(
            'host:get', self.get_json, '/hosts/%s' % '12345678',
            expect_errors=True)


class TestHostStats(api_base.FunctionalTest):
    """GET /v1/hosts/{host}/stats: one host, read when a collector asks."""

    V156 = {'OpenStack-API-Version': 'container 1.56'}

    def setUp(self):
        super(TestHostStats, self).setUp()
        test_host = utils.get_test_compute_node()
        test_host['numa_topology'] = numa.NUMATopology._from_dict(
            test_host['numa_topology'])
        self.host = objects.ComputeNode(self.context, **test_host)
        p = mock.patch('zun.objects.ComputeNode.get_by_uuid',
                       return_value=self.host)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch('zun.common.policy.enforce', return_value=True)
        p.start()
        self.addCleanup(p.stop)

    @mock.patch('zun.compute.api.API.host_sample')
    def test_one_call_for_the_host_with_what_was_asked(self, mock_sample):
        mock_sample.return_value = {'results': [], 'unavailable': []}

        response = self.get(
            '/v1/hosts/%s/stats?type=capsule&label=metering.autoscale'
            '&interval=1' % self.host.uuid, headers=dict(self.V156))

        self.assertEqual(200, response.status_int)
        self.assertEqual(self.host.hostname, response.json['host'])
        mock_sample.assert_called_once_with(
            mock.ANY, self.host.hostname, types=['capsule'],
            label='metering.autoscale', interval=1)

    @mock.patch('zun.compute.api.API.host_sample')
    def test_containers_and_capsules_by_default(self, mock_sample):
        mock_sample.return_value = {'results': [], 'unavailable': []}

        self.get('/v1/hosts/%s/stats' % self.host.uuid,
                 headers=dict(self.V156))

        self.assertEqual(['container', 'capsule'],
                         mock_sample.call_args.kwargs['types'])

    def test_needs_1_56(self):
        response = self.get(
            '/v1/hosts/%s/stats' % self.host.uuid,
            headers={'OpenStack-API-Version': 'container 1.55'},
            expect_errors=True)

        self.assertEqual(406, response.status_int)

    def test_an_unknown_type_is_refused(self):
        response = self.get('/v1/hosts/%s/stats?type=vm' % self.host.uuid,
                            headers=dict(self.V156), expect_errors=True)

        self.assertEqual(400, response.status_int)

    def test_an_interval_out_of_range_is_refused(self):
        response = self.get(
            '/v1/hosts/%s/stats?interval=60' % self.host.uuid,
            headers=dict(self.V156), expect_errors=True)

        self.assertEqual(400, response.status_int)


class TestHostStatsDefaultPolicy(api_base.FunctionalTest):
    """A collector needs the service role, not admin."""

    def _allowed(self, roles):
        from zun.common import context as zun_context
        from zun.common import policy
        ctx = zun_context.RequestContext(user_id='u', project_id='p',
                                         roles=roles)
        return policy.enforce(ctx, 'host:stats', action='host:stats',
                              do_raise=False)

    def test_the_service_role_is_enough(self):
        self.assertTrue(self._allowed(['service']))

    def test_admin_may_too(self):
        self.assertTrue(self._allowed(['admin']))

    def test_a_tenant_may_not(self):
        self.assertFalse(self._allowed(['member', 'reader']))

    def test_a_collector_may_list_the_hosts_it_polls(self):
        from zun.common import context as zun_context
        from zun.common import policy
        ctx = zun_context.RequestContext(user_id='u', project_id='p',
                                         roles=['service'])
        self.assertTrue(policy.enforce(ctx, 'host:get_all',
                                       action='host:get_all',
                                       do_raise=False))
