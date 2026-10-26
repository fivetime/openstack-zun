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

"""An image named by digest is pulled and run by that digest.

The digest used to be dropped on the way: "repo@sha256:..." became repo
plus the default tag, so the pull asked for repo:latest -- an error where
the registry has no such tag, a different image where it does.
"""

from unittest import mock

from docker import errors

from zun.common import utils
from zun.container.docker import driver as container_driver
from zun.container.docker import utils as docker_utils
from zun.image.docker import driver as image_driver
from zun.tests import base
from zun.tests.unit.container import base as container_base
from zun.tests.unit.objects import utils as obj_utils

DIGEST = 'sha256:' + '9d69ee78' * 8
REPO = 'ghcr.io/fivetime/openstackhelm/qinling-runtime-python3'


class ParseTest(base.BaseTestCase):

    def test_a_digest_is_kept(self):
        self.assertEqual((REPO, DIGEST),
                         utils.parse_image_name(REPO + '@' + DIGEST,
                                                driver='docker'))

    def test_a_digest_outranks_a_tag_given_with_it(self):
        self.assertEqual((REPO, DIGEST),
                         utils.parse_image_name(REPO + ':3.14@' + DIGEST,
                                                driver='docker'))

    def test_tags_are_as_before(self):
        self.assertEqual((REPO, '3.14'),
                         utils.parse_image_name(REPO + ':3.14',
                                                driver='docker'))
        self.assertEqual((REPO, 'latest'),
                         utils.parse_image_name(REPO, driver='docker'))

    def test_the_parts_join_back_into_the_reference(self):
        for ref in (REPO + '@' + DIGEST, REPO + ':3.14',
                    'harbor.example:5000/a/b:1'):
            self.assertEqual(
                ref, utils.image_reference(
                    *utils.parse_image_name(ref, driver='docker')))
        self.assertEqual(REPO, utils.image_reference(REPO, ''))


class PullTest(base.BaseTestCase):

    def setUp(self):
        super(PullTest, self).setUp()
        p = mock.patch.object(docker_utils, 'docker_client')
        self.docker = p.start().return_value.__enter__.return_value
        self.addCleanup(p.stop)
        self.docker.inspect_image.side_effect = errors.NotFound('no')

    def test_the_daemon_is_asked_for_the_digest(self):
        repo, tag = utils.parse_image_name(REPO + '@' + DIGEST,
                                           driver='docker')
        image_driver.DockerDriver().pull_image(
            None, repo, tag,
            utils.get_image_pull_policy(None, tag), None)

        self.docker.inspect_image.assert_called_once_with(REPO + '@' + DIGEST)
        self.docker.pull.assert_called_once_with(REPO, tag=DIGEST,
                                                 auth_config=None)


class CreateTest(container_base.DriverTestCase):

    @mock.patch('zun.container.docker.driver.DockerDriver.'
                '_get_host_storage_info')
    def setUp(self, mock_get):
        super(CreateTest, self).setUp()
        self.driver = container_driver.DockerDriver()
        p = mock.patch.object(docker_utils, 'docker_client')
        self.mock_docker = p.start().return_value.__enter__.return_value
        self.addCleanup(p.stop)

    @mock.patch('zun.network.kuryr_network.KuryrNetwork'
                '.connect_container_to_network')
    @mock.patch('zun.network.neutron.NeutronAPI.create_or_update_port')
    @mock.patch('zun.common.utils.get_security_group_ids')
    @mock.patch('zun.objects.container.Container.save')
    def test_the_container_runs_the_digest(self, mock_save, mock_sgs,
                                           mock_port, mock_connect):
        self.mock_docker.create_container = mock.Mock(
            return_value={'Id': 'abc'})
        self.mock_docker.inspect_container = mock.Mock(
            return_value={'State': 'created',
                          'Config': {'Cmd': ['fake_command']}})
        mock_port.return_value = ([], {'mac_address': 'fake_mac'})
        repo, tag = utils.parse_image_name(REPO + '@' + DIGEST,
                                           driver='docker')
        image = {'path': '', 'image': repo, 'repo': repo, 'tag': tag}
        container = obj_utils.get_test_container(self.context)
        container.status = 'Creating'
        container.healthcheck = {}
        self.driver._host.sp_disk_quota = True
        self.driver._host.storage_driver = 'overlay2'

        self.driver.create(self.context, container, image,
                           [{'network': 'fake-network'}], {})

        self.assertEqual(REPO + '@' + DIGEST,
                         self.mock_docker.create_container.call_args.args[0])
