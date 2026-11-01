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

import json
from unittest import mock
from unittest.mock import patch

from oslo_utils import uuidutils
from webtest.app import AppError

from zun.api.controllers.v1 import capsules
from zun.common import consts
from zun.common import exception
from zun.common import policies
from zun import objects
from zun.tests.unit.api import base as api_base
from zun.tests.unit.db import utils


class TestCheckCapsuleTemplate(api_base.FunctionalTest):
    def test_check_capsule_template(self):
        with self.assertRaisesRegex(
            exception.InvalidCapsuleTemplate, "kind fields need to "
                                              "be set as capsule or Capsule"):
            params = ({"kind": "test", "metadata": {},
                       "spec": {"containers": []}})
            capsules.check_capsule_template(params)

        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate, "No Spec found"):
            params = ({"kind": "capsule", "metadata": {}})
            capsules.check_capsule_template(params)

        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate,
                "No valid containers field"):
            params = ({"kind": "capsule", "metadata": {}, "spec": {}})
            capsules.check_capsule_template(params)

        params = ({"kind": "capsule", "metadata": {}, "spec": {
            "containers": [{"image": "test1"}], "restartPolicy": "Always",
        }})
        spec_content, tpl_json = capsules.check_capsule_template(params)
        self.assertEqual(spec_content["restart_policy"], "always")

    def test_check_capsule_template_unicode(self):
        with self.assertRaisesRegex(
                exception.SchemaValidationError,
                "Invalid input for field 'kind'"):
            params = ('{"kind": "test", "metadata": {}, '
                      '"spec": {"containers": []}}')
            capsules.check_capsule_template(params)

        with self.assertRaisesRegex(
                exception.SchemaValidationError,
                "'spec' is a required property"):
            params = ('{"kind": "capsule", "metadata": {}}')
            capsules.check_capsule_template(params)

        with self.assertRaisesRegex(
                exception.SchemaValidationError,
                "Invalid input for field 'spec'"):
            params = ('{"kind": "capsule", "spec": {}, "metadata": {}}')
            capsules.check_capsule_template(params)

        params = ('{"kind": "capsule", "metadata": {}, "spec": {'
                  '"containers": [{"image": "test1"}],'
                  '"restartPolicy": "Always"}}')
        spec_content, tpl_json = capsules.check_capsule_template(params)
        self.assertEqual(spec_content["restart_policy"], "always")


class TestCapsuleController(api_base.FunctionalTest):
    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule(self, mock_neutron_get_network,
                            mock_container_create):
        params = ('{'
                  '"template": '
                  '{"kind": "capsule",'
                  ' "spec": {'
                  '  "containers":'
                  '  [{"env": {"ROOT_PASSWORD": "foo0"}, '
                  '    "name": "test-container", '
                  '    "image": "test",'
                  '    "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}'
                  '  }]'
                  ' }, '
                  ' "metadata": {"labels": {"foo0": "bar0", "foo1": "bar1"},'
                  '              "name": "capsule-example"}'
                  ' }'
                  '}')
        response = self.post('/v1/capsules/',
                             params=params,
                             content_type='application/json')
        return_value = response.json
        expected_meta_name = "capsule-example"
        expected_meta_labels = {"foo0": "bar0", "foo1": "bar1"}
        expected_memory = '1024'
        expected_cpu = 1.0
        self.assertEqual(return_value["name"], expected_meta_name)
        self.assertEqual(return_value["labels"], expected_meta_labels)
        self.assertEqual(return_value["memory"], expected_memory)
        self.assertEqual(return_value["cpu"], expected_cpu)
        self.assertEqual(202, response.status_int)
        self.assertTrue(mock_container_create.called)
        self.assertTrue(mock_neutron_get_network.called)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule_two_containers(self, mock_neutron_get_network,
                                           mock_container_create):
        params = ('{'
                  '"template": '
                  '{"kind": "capsule",'
                  ' "spec": {'
                  '  "containers":'
                  '  [{"image": "test", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}, '
                  '   {"image": "test1", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}]'
                  ' }, '
                  ' "metadata": {"labels": {"foo0": "bar0"},'
                  '              "name": "capsule-example"}'
                  ' }'
                  '}')
        response = self.post('/v1/capsules/',
                             params=params,
                             content_type='application/json')
        return_value = response.json
        expected_meta_name = "capsule-example"
        expected_meta_labels = {"foo0": "bar0"}
        expected_memory = '2048'
        expected_cpu = 2.0
        self.assertEqual(return_value["name"],
                         expected_meta_name)
        self.assertEqual(return_value["labels"],
                         expected_meta_labels)
        self.assertEqual(return_value["memory"], expected_memory)
        self.assertEqual(return_value["cpu"], expected_cpu)
        self.assertEqual(202, response.status_int)
        self.assertTrue(mock_container_create.called)
        self.assertTrue(mock_neutron_get_network.called)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.api.controllers.v1.capsules.check_capsule_template')
    def test_create_capsule_wrong_kind_set(self, mock_check_template,
                                           mock_container_create):
        params = ('{"template": {"kind": "test",'
                  '"spec": {"containers":'
                  '[{"environment": {"ROOT_PASSWORD": "foo0"}, '
                  '"image": "test1", "resources": '
                  '{"requests": {"cpu": 1, "memory": 1024}}}]}, '
                  '"metadata": {"labels": {"foo0": "bar0"}, '
                  '"name": "capsule-example"}}}')
        mock_check_template.side_effect = exception.InvalidCapsuleTemplate(
            "kind fields need to be set as capsule or Capsule")
        response = self.post_json('/capsules/', params, expect_errors=True)
        self.assertEqual(400, response.status_int)
        self.assertFalse(mock_container_create.called)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.api.controllers.v1.capsules.check_capsule_template')
    def test_create_capsule_less_than_one_container(self, mock_check_template,
                                                    mock_container_create):
        params = ('{"template": {"kind": "capsule",'
                  '"spec": {container:[]}, '
                  '"metadata": {"labels": {"foo0": "bar0"}, '
                  '"name": "capsule-example"}}}')
        mock_check_template.side_effect = exception.InvalidCapsuleTemplate(
            "Capsule need to have one container at least")
        response = self.post_json('/capsules/', params, expect_errors=True)
        self.assertEqual(400, response.status_int)
        self.assertFalse(mock_container_create.called)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.api.controllers.v1.capsules.check_capsule_template')
    def test_create_capsule_no_container_field(self, mock_check_template,
                                               mock_container_create):
        params = ('{"template": {"kind": "capsule",'
                  '"spec": {}, '
                  '"metadata": {"labels": {"foo0": "bar0"}, '
                  '"name": "capsule-example"}}}')
        mock_check_template.side_effect = exception.InvalidCapsuleTemplate(
            "Capsule need to have one container at least")
        self.assertRaises(AppError, self.post, '/capsules/',
                          params=params, content_type='application/json')
        self.assertFalse(mock_container_create.called)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.api.controllers.v1.capsules.check_capsule_template')
    def test_create_capsule_no_container_image(self, mock_check_template,
                                               mock_container_create):
        params = ('{"template": {"kind": "capsule",'
                  '"spec": {container:[{"env": '
                  '{"ROOT_PASSWORD": "foo1"}]}, '
                  '"metadata": {"labels": {"foo0": "bar0"}, '
                  '"name": "capsule-example"}}}')
        mock_check_template.side_effect = exception.InvalidCapsuleTemplate(
            "Container image is needed")
        self.assertRaises(AppError, self.post, '/v1/capsules/',
                          params=params, content_type='application/json')
        self.assertFalse(mock_container_create.called)

    def _post_privileged(self, member):
        spec = {'containers': [{'image': 'test'}]}
        privileged = {'image': 'test-p',
                      'securityContext': {'privileged': True}}
        if member == 'init':
            spec['initContainers'] = [privileged]
        else:
            spec['containers'] = [privileged]
        return self.post_json(
            '/capsules/',
            {'template': {'kind': 'capsule', 'spec': spec,
                          'metadata': {'name': 'capsule-example'}}},
            expect_errors=True)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_privileged_refused_by_policy_leaves_nothing(
            self, mock_neutron_get_network, mock_container_create):
        # capsule:create too: set_rules replaces, and a 403 from a missing
        # rule would pass this for the wrong reason.
        self.policy.set_rules({'capsule:create': '@',
                               'capsule:create:privileged': '!'})
        for member in ('container', 'init'):
            response = self._post_privileged(member)
            self.assertEqual(403, response.status_int, member)
        self.assertFalse(mock_container_create.called)
        self.assertEqual(
            [], objects.Capsule.list(self.context))

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_privileged_allowed_by_policy_reaches_the_record(
            self, mock_neutron_get_network, mock_container_create):
        self.policy.set_rules({'capsule:create': '@',
                               'capsule:create:privileged': '@'})
        for member, field in (('container', 'containers'),
                              ('init', 'init_containers')):
            response = self._post_privileged(member)
            self.assertEqual(202, response.status_int, member)
            capsule = mock_container_create.call_args[0][1]
            self.assertIs(True, getattr(capsule, field)[0].privileged)

    def test_privileged_rule_defaults_to_admin(self):
        rules = {r.name: r.check_str for r in policies.list_rules()}
        self.assertEqual('rule:context_is_admin',
                         rules['capsule:create:privileged'])

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule_init_container_is_validated(
            self, mock_neutron_get_network, mock_container_create):
        # The same fields as a main container: unlisted in the schema, these
        # went straight into the record, and the driver honours privileged.
        for field in ('"privileged": true', '"restartPolicy": "OnFailure"',
                      '"runtime": "runc"'):
            params = ('{"template": {"kind": "capsule", "spec": {'
                      ' "initContainers": [{"image": "test-init", %s}],'
                      ' "containers": [{"image": "test"}]},'
                      ' "metadata": {"name": "capsule-example"}}}' % field)
            response = self.post('/v1/capsules/', params=params,
                                 content_type='application/json',
                                 expect_errors=True)
            self.assertEqual(400, response.status_int, field)
        self.assertFalse(mock_container_create.called)

    def _post_with_init(self, init, containers=None, policy='Always'):
        return self.post(
            '/v1/capsules/',
            params=json.dumps({'template': {
                'kind': 'capsule',
                'metadata': {'name': 'capsule-example'},
                'spec': {'restartPolicy': policy,
                         'initContainers': init,
                         'containers': containers or [{'image': 'test'}]},
            }}),
            content_type='application/json', expect_errors=True)

    @staticmethod
    def _res(cpu, memory):
        return {'requests': {'cpu': cpu, 'memory': memory}}

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_a_sidecar_is_recorded_by_its_restart_policy(
            self, mock_neutron_get_network, mock_container_create):
        response = self._post_with_init(
            [{'image': 'plain'},
             {'image': 'sidecar', 'restartPolicy': 'Always'}])
        self.assertEqual(202, response.status_int)
        capsule = objects.Capsule.list(self.context)[0]
        by_image = {c.image: c.restart_policy
                    for c in capsule.init_containers}
        self.assertEqual({'MaximumRetryCount': '0', 'Name': 'always'},
                         by_image['sidecar'])
        self.assertEqual({'MaximumRetryCount': '10', 'Name': 'on-failure'},
                         by_image['plain'])

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_an_init_container_keeps_its_policy_to_itself(
            self, mock_neutron_get_network, mock_container_create):
        # It reached the main container, whose `always` became on-failure:10.
        self.assertEqual(202, self._post_with_init(
            [{'image': 'plain'}]).status_int)
        capsule = objects.Capsule.list(self.context)[0]
        self.assertEqual({'MaximumRetryCount': '0', 'Name': 'always'},
                         capsule.containers[0].restart_policy)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_probes_are_for_sidecars(
            self, mock_neutron_get_network, mock_container_create):
        probe = {'exec': {'command': ['true']}}
        response = self._post_with_init(
            [{'image': 'plain', 'startupProbe': probe}])
        self.assertEqual(400, response.status_int)
        self.assertFalse(mock_container_create.called)
        response = self._post_with_init(
            [{'image': 'sidecar', 'restartPolicy': 'Always',
              'startupProbe': probe}])
        self.assertEqual(202, response.status_int)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_a_sidecar_counts_as_kubernetes_counts_it(
            self, mock_neutron_get_network, mock_container_create):
        # The pod resources formula: sidecar 1/100 runs beside main 2/200,
        # and beside the init container 4/1024 that starts after it.
        response = self._post_with_init(
            [{'image': 'sidecar', 'restartPolicy': 'Always',
              'resources': self._res(1, 100)},
             {'image': 'plain', 'resources': self._res(4, 1024)}],
            [{'image': 'main', 'resources': self._res(2, 200)}])
        self.assertEqual(202, response.status_int)
        capsule = objects.Capsule.list(self.context)[0]
        self.assertEqual(5, capsule.cpu)
        self.assertEqual('1124', capsule.memory.rstrip('M'))

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule_with_init_containers(self,
                                                 mock_neutron_get_network,
                                                 mock_container_create):
        params = ('{'
                  '"template": '
                  '{"kind": "capsule",'
                  ' "spec": {'
                  '  "initContainers":'
                  '  [{"image": "test-init", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}],'
                  '  "containers":'
                  '  [{"image": "test", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}, '
                  '   {"image": "test1", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}]'
                  ' }, '
                  ' "metadata": {"labels": {"foo0": "bar0"},'
                  '              "name": "capsule-example"}'
                  ' }'
                  '}')
        response = self.post('/v1/capsules/',
                             params=params,
                             content_type='application/json')
        return_value = response.json
        expected_meta_name = "capsule-example"
        expected_meta_labels = {"foo0": "bar0"}
        expected_memory = '2048'
        expected_cpu = 2.0

        self.assertEqual(return_value["name"],
                         expected_meta_name)
        self.assertEqual(return_value["labels"],
                         expected_meta_labels)
        self.assertEqual(return_value["memory"], expected_memory)
        self.assertEqual(return_value["cpu"], expected_cpu)
        self.assertEqual(202, response.status_int)
        self.assertTrue(mock_container_create.called)
        self.assertTrue(mock_neutron_get_network.called)

    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule_with_two_init_containers(self,
                                                     mock_neutron_get_network,
                                                     mock_container_create):
        params = ('{'
                  '"template": '
                  '{"kind": "capsule",'
                  ' "spec": {'
                  '  "containers":'
                  '  [{"image": "test", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}],'
                  '  "initContainers":'
                  '  [{"image": "init-test", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}, '
                  '   {"image": "init-test1", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}}}]'
                  ' }, '
                  ' "metadata": {"labels": {"foo0": "bar0"},'
                  '              "name": "capsule-example"}'
                  ' }'
                  '}')
        response = self.post('/v1/capsules/',
                             params=params,
                             content_type='application/json')
        return_value = response.json
        expected_meta_name = "capsule-example"
        expected_meta_labels = {"foo0": "bar0"}
        expected_memory = '1024'
        expected_cpu = 1.0
        self.assertEqual(return_value["name"],
                         expected_meta_name)
        self.assertEqual(return_value["labels"],
                         expected_meta_labels)
        self.assertEqual(return_value["memory"], expected_memory)
        self.assertEqual(return_value["cpu"], expected_cpu)
        self.assertEqual(202, response.status_int)
        self.assertTrue(mock_container_create.called)
        self.assertTrue(mock_neutron_get_network.called)

    @patch('zun.volume.cinder_api.CinderAPI.ensure_volume_usable')
    @patch('zun.volume.cinder_api.CinderAPI.create_volume')
    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule_with_create_new_volume(self,
                                                   mock_neutron_get_network,
                                                   mock_container_create,
                                                   mock_create_volume,
                                                   mock_ensure_volume_usable):
        fake_volume_id = '3259309d-659c-4e20-b354-ee712e64b3b2'
        fake_volume = mock.Mock(id=fake_volume_id)
        mock_create_volume.return_value = fake_volume
        params = ('{'
                  '"template":'
                  '{"kind": "capsule",'
                  ' "spec":'
                  ' {"containers":'
                  '  [{"env": {"ROOT_PASSWORD": "foo0"}, '
                  '    "image": "test", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}},'
                  '     "volumeMounts": [{"name": "volume1", '
                  '                       "mountPath": "/data1"}]'
                  '   }'
                  '  ],'
                  '  "volumes":'
                  '  [{"name": "volume1",'
                  '    "cinder": {"size": 3, "autoRemove": "True"}'
                  '  }]'
                  ' }, '
                  ' "metadata": {"labels": '
                  '             {"foo0": "bar0", "foo1": "bar1"},'
                  '              "name": "capsule-example"}'
                  ' }'
                  '}')
        response = self.post('/v1/capsules/',
                             params=params,
                             content_type='application/json')
        return_value = response.json
        expected_meta_name = "capsule-example"
        expected_meta_labels = {"foo0": "bar0", "foo1": "bar1"}
        expected_memory = '1024'
        expected_cpu = 1.0
        self.assertEqual(return_value["name"], expected_meta_name)
        self.assertEqual(return_value["labels"], expected_meta_labels)
        self.assertEqual(return_value["memory"], expected_memory)
        self.assertEqual(return_value["cpu"], expected_cpu)
        self.assertEqual(202, response.status_int)
        self.assertTrue(mock_container_create.called)
        self.assertTrue(mock_neutron_get_network.called)
        self.assertTrue(mock_create_volume.called)

    @patch('zun.volume.cinder_api.CinderAPI.ensure_volume_usable')
    @patch('zun.volume.cinder_api.CinderAPI.search_volume')
    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule_with_existed_volume(self, mock_neutron_get_network,
                                                mock_container_create,
                                                mock_search_volume,
                                                mock_ensure_volume_usable):
        fake_volume_id = '3259309d-659c-4e20-b354-ee712e64b3b2'
        fake_volume = mock.Mock(id=fake_volume_id)
        mock_search_volume.return_value = fake_volume
        params = ('{'
                  '"template":'
                  '{"kind": "capsule",'
                  ' "spec":'
                  ' {"containers":'
                  '  [{"env": {"ROOT_PASSWORD": "foo0"}, '
                  '    "image": "test", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}},'
                  '     "volumeMounts": [{"name": "volume1", '
                  '                       "mountPath": "/data1"}]'
                  '   }'
                  '  ],'
                  '  "volumes":'
                  '  [{"name": "volume1",'
                  '    "cinder": {"volumeID": '
                  '    "3259309d-659c-4e20-b354-ee712e64b3b2"}'
                  '  }]'
                  ' }, '
                  ' "metadata": {"labels": '
                  '              {"foo0": "bar0", "foo1": "bar1"},'
                  '              "name": "capsule-example"}'
                  ' }'
                  '}')
        response = self.post('/v1/capsules/',
                             params=params,
                             content_type='application/json')
        return_value = response.json
        expected_meta_name = "capsule-example"
        expected_meta_labels = {"foo0": "bar0", "foo1": "bar1"}
        expected_memory = '1024'
        expected_cpu = 1.0
        self.assertEqual(return_value["name"], expected_meta_name)
        self.assertEqual(return_value["labels"], expected_meta_labels)
        self.assertEqual(return_value["memory"], expected_memory)
        self.assertEqual(return_value["cpu"], expected_cpu)
        self.assertEqual(202, response.status_int)
        self.assertTrue(mock_container_create.called)
        self.assertTrue(mock_neutron_get_network.called)
        self.assertTrue(mock_ensure_volume_usable.called)
        self.assertTrue(mock_search_volume.called)

    @patch('zun.volume.cinder_api.CinderAPI.create_volume')
    @patch('zun.volume.cinder_api.CinderAPI.ensure_volume_usable')
    @patch('zun.volume.cinder_api.CinderAPI.search_volume')
    @patch('zun.compute.api.API.container_create')
    @patch('zun.network.neutron.NeutronAPI.get_available_network')
    def test_create_capsule_with_two_volumes(self, mock_neutron_get_network,
                                             mock_container_create,
                                             mock_search_volume,
                                             mock_ensure_volume_usable,
                                             mock_create_volume):
        fake_volume_id1 = '3259309d-659c-4e20-b354-ee712e64b3b2'
        fake_volume = mock.Mock(id=fake_volume_id1)
        mock_search_volume.return_value = fake_volume
        fake_volume_id2 = 'ef770cfb-349a-483a-97f6-b86e46e344b8'
        fake_volume = mock.Mock(id=fake_volume_id2)
        mock_create_volume.return_value = fake_volume
        params = ('{'
                  '"template":'
                  '{"kind": "capsule",'
                  ' "spec":'
                  ' {"containers":'
                  '  [{"env": {"ROOT_PASSWORD": "foo0"}, '
                  '    "image": "test", "resources": '
                  '    {"requests": {"cpu": 1, "memory": 1024}},'
                  '     "volumeMounts": [{"name": "volume1", '
                  '                       "mountPath": "/data1"},'
                  '                       {"name": "volume2", '
                  '                       "mountPath": "/data2"}]'
                  '   }'
                  '  ],'
                  '  "volumes":'
                  '  [{"name": "volume1",'
                  '    "cinder": {"volumeID": '
                  '    "3259309d-659c-4e20-b354-ee712e64b3b2"}},'
                  '   {"name": "volume2",'
                  '    "cinder": {"size": 3, "autoRemove": "True"}'
                  '  }]'
                  ' }, '
                  ' "metadata": {"labels": {"foo0": "bar0", "foo1": "bar1"},'
                  '              "name": "capsule-example"}'
                  ' }'
                  '}')
        response = self.post('/v1/capsules/',
                             params=params,
                             content_type='application/json')
        return_value = response.json
        expected_meta_name = "capsule-example"
        expected_meta_labels = {"foo0": "bar0", "foo1": "bar1"}
        expected_memory = '1024'
        expected_cpu = 1.0
        self.assertEqual(return_value["name"], expected_meta_name)
        self.assertEqual(return_value["labels"], expected_meta_labels)
        self.assertEqual(return_value["memory"], expected_memory)
        self.assertEqual(return_value["cpu"], expected_cpu)
        self.assertEqual(202, response.status_int)
        self.assertTrue(mock_container_create.called)
        self.assertTrue(mock_neutron_get_network.called)
        self.assertTrue(mock_ensure_volume_usable.called)
        self.assertTrue(mock_search_volume.called)
        self.assertTrue(mock_create_volume.called)

    @patch('zun.compute.api.API.container_show')
    @patch('zun.objects.Capsule.get_by_uuid')
    @patch('zun.objects.Container.get_by_uuid')
    def test_get_one_by_uuid(self, mock_container_get_by_uuid,
                             mock_capsule_get_by_uuid,
                             mock_container_show):
        test_container = utils.get_test_container()
        test_container_obj = objects.Container(self.context, **test_container)
        mock_container_get_by_uuid.return_value = test_container_obj
        mock_container_show.return_value = test_container_obj

        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context, **test_capsule)
        mock_capsule_get_by_uuid.return_value = test_capsule_obj

        response = self.get('/v1/capsules/%s/' % test_capsule['uuid'])

        context = mock_capsule_get_by_uuid.call_args[0][0]
        self.assertIs(False, context.all_projects)
        self.assertEqual(200, response.status_int)
        self.assertEqual(test_capsule['uuid'],
                         response.json['uuid'])

    @patch('zun.compute.api.API.container_show')
    @patch('zun.objects.Capsule.get_by_uuid')
    @patch('zun.objects.Container.get_by_uuid')
    def test_get_one_by_uuid_all_projects(self, mock_container_get_by_uuid,
                                          mock_capsule_get_by_uuid,
                                          mock_container_show):
        test_container = utils.get_test_container()
        test_container_obj = objects.Container(self.context, **test_container)
        mock_container_get_by_uuid.return_value = test_container_obj
        mock_container_show.return_value = test_container_obj

        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context, **test_capsule)
        mock_capsule_get_by_uuid.return_value = test_capsule_obj

        response = self.get('/capsules/%s/?all_projects=1' %
                            test_capsule['uuid'])

        context = mock_capsule_get_by_uuid.call_args[0][0]
        self.assertIs(False, context.all_projects)
        self.assertEqual(200, response.status_int)
        self.assertEqual(test_capsule['uuid'],
                         response.json['uuid'])

    @patch('zun.compute.api.API.container_delete')
    @patch('zun.objects.Capsule.get_by_uuid')
    @patch('zun.objects.Capsule.save')
    def test_delete_capsule_by_uuid(self, mock_capsule_save,
                                    mock_capsule_get_by_uuid,
                                    mock_capsule_delete):
        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context,
                                           **test_capsule)
        mock_capsule_get_by_uuid.return_value = test_capsule_obj
        mock_capsule_save.return_value = True
        mock_capsule_delete.return_value = True

        capsule_uuid = test_capsule.get('uuid')
        response = self.app.delete('/v1/capsules/%s' % capsule_uuid)

        self.assertTrue(mock_capsule_delete.called)
        self.assertEqual(204, response.status_int)
        # No body at all on a 204: a gateway in front (Envoy) turns one
        # with a body into a 502, though the delete went through.
        self.assertEqual(b'', response.body)
        context = mock_capsule_save.call_args[0][0]
        self.assertIs(False, context.all_projects)

    @patch('zun.common.policy.enforce')
    @patch('zun.compute.api.API.container_delete')
    @patch('zun.objects.Capsule.get_by_uuid')
    @patch('zun.objects.Capsule.save')
    def test_delete_capsule_by_uuid_all_projects(self,
                                                 mock_capsule_save,
                                                 mock_capsule_get_by_uuid,
                                                 mock_capsule_delete,
                                                 mock_policy):
        mock_policy.return_value = True
        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context,
                                           **test_capsule)
        mock_capsule_get_by_uuid.return_value = test_capsule_obj
        mock_capsule_save.return_value = True
        mock_capsule_delete.return_value = True

        capsule_uuid = test_capsule.get('uuid')
        response = self.app.delete(
            '/v1/capsules/%s/?all_projects=1' % capsule_uuid)

        self.assertTrue(mock_capsule_delete.called)
        self.assertEqual(204, response.status_int)
        context = mock_capsule_save.call_args[0][0]
        self.assertIs(True, context.all_projects)

    def test_delete_capsule_with_uuid_not_found(self):
        uuid = uuidutils.generate_uuid()
        self.assertRaises(AppError, self.app.delete,
                          '/capsules/%s' % uuid)

    @patch('zun.compute.api.API.container_delete')
    @patch('zun.objects.Capsule.get_by_name')
    @patch('zun.objects.Capsule.save')
    def test_delete_capsule_by_name(self, mock_capsule_save,
                                    mock_capsule_get_by_name,
                                    mock_capsule_delete):
        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context,
                                           **test_capsule)
        mock_capsule_get_by_name.return_value = test_capsule_obj
        mock_capsule_save.return_value = True
        mock_capsule_delete.return_value = True

        capsule_name = test_capsule.get('name')
        response = self.app.delete('/v1/capsules/%s/' %
                                   capsule_name)

        self.assertTrue(mock_capsule_delete.called)
        self.assertEqual(204, response.status_int)
        context = mock_capsule_save.call_args[0][0]
        self.assertIs(False, context.all_projects)

    @patch('zun.compute.api.API.container_show')
    @patch('zun.objects.Capsule.list')
    @patch('zun.objects.Container.get_by_uuid')
    def test_get_all_capsules(self, mock_container_get_by_uuid,
                              mock_capsule_list,
                              mock_container_show):
        test_container = utils.get_test_container()
        test_container_obj = objects.Container(self.context,
                                               **test_container)
        mock_container_get_by_uuid.return_value = test_container_obj

        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context, **test_capsule)
        mock_capsule_list.return_value = [test_capsule_obj]
        mock_container_show.return_value = test_container_obj

        response = self.app.get('/v1/capsules/')

        mock_capsule_list.assert_called_once_with(mock.ANY,
                                                  1000, None, 'id', 'asc',
                                                  filters=None)
        context = mock_capsule_list.call_args[0][0]
        self.assertIs(False, context.all_projects)
        self.assertEqual(200, response.status_int)
        actual_capsules = response.json['capsules']
        self.assertEqual(1, len(actual_capsules))
        self.assertEqual(test_capsule['uuid'],
                         actual_capsules[0].get('uuid'))

    @patch('zun.compute.api.API.container_show')
    @patch('zun.objects.Capsule.list')
    @patch('zun.objects.Container.get_by_uuid')
    def test_get_all_capsules_all_projects(self,
                                           mock_container_get_by_uuid,
                                           mock_capsule_list,
                                           mock_container_show):
        test_container = utils.get_test_container()
        test_container_obj = objects.Container(self.context,
                                               **test_container)
        mock_container_get_by_uuid.return_value = test_container_obj

        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context, **test_capsule)
        mock_capsule_list.return_value = [test_capsule_obj]
        mock_container_show.return_value = test_container_obj

        response = self.app.get('/v1/capsules/?all_projects=1')

        mock_capsule_list.assert_called_once_with(mock.ANY,
                                                  1000, None, 'id', 'asc',
                                                  filters=None)
        context = mock_capsule_list.call_args[0][0]
        self.assertIs(True, context.all_projects)
        self.assertEqual(200, response.status_int)
        actual_capsules = response.json['capsules']
        self.assertEqual(1, len(actual_capsules))
        self.assertEqual(test_capsule['uuid'],
                         actual_capsules[0].get('uuid'))

    @patch('zun.objects.Capsule.list')
    @patch('zun.objects.Container.get_by_uuid')
    def test_get_all_capsules_with_exception(self,
                                             mock_container_get_by_uuid,
                                             mock_capsule_list):
        test_container = utils.get_test_container()
        test_container_obj = objects.Container(self.context,
                                               **test_container)
        mock_container_get_by_uuid.return_value = test_container_obj

        test_capsule = utils.create_test_container(context=self.context)
        test_capsule_obj = objects.Capsule(self.context, **test_capsule)
        mock_capsule_list.return_value = [test_capsule_obj]

        response = self.app.get('/v1/capsules/')

        mock_capsule_list.assert_called_once_with(mock.ANY,
                                                  1000, None, 'id', 'asc',
                                                  filters=None)
        context = mock_capsule_list.call_args[0][0]
        self.assertIs(False, context.all_projects)
        self.assertEqual(200, response.status_int)
        actual_capsules = response.json['capsules']
        self.assertEqual(1, len(actual_capsules))
        self.assertEqual(test_capsule['uuid'],
                         actual_capsules[0].get('uuid'))

    @patch('zun.compute.api.API.container_show')
    @patch('zun.objects.Capsule.list')
    @patch('zun.objects.Capsule.save')
    @patch('zun.objects.Capsule.get_by_uuid')
    def test_get_all_capsules_with_pagination_marker(
            self,
            mock_container_get_by_uuid,
            mock_capsule_save,
            mock_capsule_list,
            mock_capsule_show):
        test_container = utils.get_test_container()
        test_container_obj = objects.Container(self.context,
                                               **test_container)
        mock_container_get_by_uuid.return_value = test_container_obj
        capsule_list = []
        for id_ in range(4):
            test_capsule = utils.create_test_container(
                id=id_, uuid=uuidutils.generate_uuid(),
                name='capsule' + str(id_), context=self.context)
            capsule_list.append(objects.Capsule(self.context,
                                                **test_capsule))
        mock_capsule_list.return_value = capsule_list[-1:]
        mock_capsule_show.return_value = capsule_list[-1]
        mock_capsule_save.return_value = True

        response = self.app.get('/v1/capsules/?limit=3&marker=%s'
                                % capsule_list[2].uuid)

        self.assertEqual(200, response.status_int)
        actual_capsules = response.json['capsules']

        self.assertEqual(1, len(actual_capsules))
        self.assertEqual(actual_capsules[-1].get('uuid'),
                         actual_capsules[0].get('uuid'))


class TestFindContainer(api_base.FunctionalTest):
    """A capsule's members are found across both lists, on real objects.

    The two list fields hold different object types, so adding them directly
    raises, for any capsule with an init container and whichever member asked
    for: mocks of them add up happily.
    """

    def _capsule(self):
        capsule = objects.Capsule(self.context, **utils.get_test_container())
        self.main = objects.CapsuleContainer(
            self.context, **utils.get_test_container(
                uuid=uuidutils.generate_uuid(), name='main'))
        self.init = objects.CapsuleInitContainer(
            self.context, **utils.get_test_container(
                uuid=uuidutils.generate_uuid(), name='init'))
        capsule.containers = [self.main]
        capsule.init_containers = [self.init]
        return capsule

    def test_a_main_container_of_a_capsule_with_an_init_container(self):
        found = capsules.CapsuleController._find_container(
            self._capsule(), 'main')
        self.assertEqual(self.main.uuid, found.uuid)

    def test_an_init_container_by_uuid(self):
        capsule = self._capsule()
        found = capsules.CapsuleController._find_container(
            capsule, self.init.uuid)
        self.assertIsInstance(found, objects.CapsuleInitContainer)

    def test_two_members_have_to_be_told_apart(self):
        self.assertRaises(exception.Invalid,
                          capsules.CapsuleController._find_container,
                          self._capsule(), None)

    def test_exec_goes_by_the_member_not_the_capsule(self):
        # An init container runs while its capsule is Creating, and a pod
        # lets you into it: that is how a stuck one gets looked at.
        response, call = self._exec_into_init()
        self.assertNotEqual(409, response.status_int)
        call.assert_called_once()

    def _exec_into_init(self):
        capsule = self._capsule()
        capsule.status = consts.CREATING
        capsule.host = 'node-1'
        self.init.status = consts.RUNNING
        capsule.init_containers = [self.init]
        with patch('zun.api.utils.get_resource', return_value=capsule), \
                patch('zun.api.controllers.v1.capsules.'
                      'check_policy_on_capsule'), \
                patch('zun.compute.api.API.container_exec',
                      return_value={'output': 'ok'}) as call:
            response = self.post(
                '/v1/capsules/%s/execute?container=%s&command=true&run=true'
                % (capsule.uuid, self.init.uuid), expect_errors=True)
        return response, call

    def test_exec_into_a_member_that_has_stopped_is_refused(self):
        capsule = self._capsule()
        capsule.status = consts.RUNNING
        capsule.host = 'node-1'
        self.main.status = consts.STOPPED
        capsule.containers = [self.main]
        with patch('zun.api.utils.get_resource', return_value=capsule), \
                patch('zun.api.controllers.v1.capsules.'
                      'check_policy_on_capsule'), \
                patch('zun.compute.api.API.container_exec') as call:
            response = self.post(
                '/v1/capsules/%s/execute?container=%s&command=true&run=true'
                % (capsule.uuid, self.main.uuid), expect_errors=True)
        self.assertEqual(409, response.status_int)
        call.assert_not_called()
