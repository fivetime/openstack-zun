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

"""A create that is refused leaves no record behind.

The container row is written before the volumes are checked, and a
volume in use is refused after. The row then stood in CREATING with no
node behind it: it held the name, could not be stopped ("in Creating
state"), and a retry with the same name was refused as a conflict with
it. Measured with compose, whose recreate makes the new container
before removing the old one that still holds the volume.
"""

from unittest import mock

from zun.common import exception
from zun import objects
from zun.tests.unit.api import base as api_base


_MOUNTED = ('{"name": "web", "image": "alpine", '
            '"mounts": [{"source": "data", "destination": "/data"}]}')


class TestARefusedCreateLeavesNoRecord(api_base.FunctionalTest):

    @mock.patch('zun.network.neutron.NeutronAPI.get_available_network')
    @mock.patch('zun.compute.api.API.container_create')
    @mock.patch('zun.compute.api.API.image_search')
    @mock.patch('zun.api.controllers.v1.containers.ContainersController.'
                '_build_requested_volumes')
    def test_the_record_is_dropped_when_a_volume_is_refused(
            self, volumes, _search, compute_create, _net):
        volumes.side_effect = exception.VolumeInUse(volume='v-1')

        response = self.post('/v1/containers/', params=_MOUNTED,
                             content_type='application/json',
                             expect_errors=True)

        self.assertEqual(400, response.status_int)
        self.assertEqual([], objects.Container.list(self.context))
        compute_create.assert_not_called()

    @mock.patch('zun.network.neutron.NeutronAPI.get_available_network')
    @mock.patch('zun.compute.api.API.container_create')
    @mock.patch('zun.compute.api.API.image_search')
    @mock.patch('zun.api.controllers.v1.containers.ContainersController.'
                '_build_requested_volumes')
    @mock.patch('zun.objects.Container.destroy')
    def test_the_refusal_itself_is_what_the_caller_hears(
            self, destroy, volumes, _search, _compute_create, _net):
        """Dropping the record must not replace the reason with its own."""
        volumes.side_effect = exception.VolumeInUse(volume='v-1')
        destroy.side_effect = RuntimeError('db away')

        response = self.post('/v1/containers/', params=_MOUNTED,
                             content_type='application/json',
                             expect_errors=True)

        self.assertEqual(400, response.status_int)
        self.assertIn('still in use', response.json['errors'][0]['detail'])

    @mock.patch('zun.network.neutron.NeutronAPI.get_available_network')
    @mock.patch('zun.compute.api.API.container_create')
    @mock.patch('zun.compute.api.API.image_search')
    def test_a_create_that_goes_through_keeps_its_record(
            self, _search, compute_create, _net):
        compute_create.side_effect = lambda x, y, **z: y

        response = self.post('/v1/containers/',
                             params='{"name": "web", "image": "alpine"}',
                             content_type='application/json')

        self.assertEqual(202, response.status_int)
        self.assertEqual(['web'],
                         [c.name for c in objects.Container.list(self.context)])
