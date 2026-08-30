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

"""What this driver has no counterpart for, said rather than raised.

The base class raises NotImplementedError, which reaches the caller as
a server error -- the one answer that says neither what happened nor
what to do about it. These paths belong to the docker driver's shape
of the world: an image service to snapshot into, and networks made on
the host before a container joins them.
"""

from unittest import mock

from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


class NoCounterpartTest(base.TestCase):

    def setUp(self):
        super(NoCounterpartTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)

    def _refused(self, call, *args):
        return self.assertRaises(exception.Invalid, call, *args)

    def test_committing_to_the_image_service_names_what_works(self):
        error = self._refused(self.driver.create_image, {}, 'repo', None)

        self.assertIn('registry', str(error))

    def test_uploading_image_data_says_the_same(self):
        error = self._refused(self.driver.upload_image_data, {}, None, 't',
                              b'', None)

        self.assertIn('registry', str(error))

    def test_deleting_a_snapshot_says_there_is_none(self):
        self._refused(self.driver.delete_committed_image, {}, 'x', None)

    def test_making_a_network_says_where_they_come_from(self):
        error = self._refused(self.driver.create_network, {}, 'net-1')

        self.assertIn('CNI', str(error))
        self.assertIn('neutron', str(error))

    def test_deleting_a_network_says_nothing_was_made_here(self):
        error = self._refused(self.driver.delete_network, {}, 'net-1')

        self.assertIn('neutron', str(error))

    def test_none_of_them_leave_a_bare_not_implemented(self):
        """NotImplementedError reaches a tenant as a 500 and says nothing."""
        for call, args in ((self.driver.create_image, ({}, 'r', None)),
                           (self.driver.create_network, ({}, 'n')),
                           (self.driver.delete_network, ({}, 'n'))):
            self.assertRaises(exception.Invalid, call, *args)


class DeleteImageTest(base.TestCase):

    def setUp(self):
        super(DeleteImageTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.image_stub = mock.Mock()

    class _Refused(cri_driver.grpc.RpcError):
        def __init__(self, code):
            super().__init__()
            self._code = code

        def code(self):
            return self._code

    def test_it_asks_the_runtime_to_remove_it(self):
        self.driver.delete_image({}, 'sha256:abc')

        self.driver.image_stub.RemoveImage.assert_called_once()

    def test_an_image_already_gone_is_not_a_failure(self):
        self.driver.image_stub.RemoveImage.side_effect = self._Refused(
            cri_driver.grpc.StatusCode.NOT_FOUND)

        self.driver.delete_image({}, 'sha256:abc')

    def test_any_other_refusal_is_passed_on(self):
        """An image a container still holds is the runtime's call to make."""
        self.driver.image_stub.RemoveImage.side_effect = self._Refused(
            cri_driver.grpc.StatusCode.FAILED_PRECONDITION)

        self.assertRaises(cri_driver.grpc.RpcError,
                          self.driver.delete_image, {}, 'sha256:abc')
