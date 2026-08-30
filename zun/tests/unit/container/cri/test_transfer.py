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

"""Handing a committed image to containerd to push.

The source and destination travel inside google.protobuf.Any, whose
type_url is built from the message's full name -- so a message declared
under a package of our own is packed as a type containerd has never
heard of and refused as an unknown destination. The package is the
contract twice over here: once for routing the call, once for naming
what is inside it.
"""

import base64
from unittest import mock

from zun.container.cri import commit_cli
from zun.criapi import ctrd_transfer_types_pb2
from zun.tests import base


class PushThroughTransferTest(base.TestCase):

    def _push(self, **extra):
        request = {'address': 'unix:///run/containerd/containerd.sock',
                   'namespace': 'k8s.io', 'snapshotter': 'overlayfs',
                   'name': 'harbor.example/p/app:v1', 'timeout': 600}
        request.update(extra)
        with mock.patch.object(commit_cli, '_Stubs') as stubs:
            answer = commit_cli.do_push(request)
        return stubs.return_value.transfer_stub.Transfer.call_args, answer

    def test_the_source_is_the_image_containerd_already_has(self):
        call, _answer = self._push()
        source = call.args[0].source

        self.assertEqual('type.googleapis.com/'
                         'containerd.types.transfer.ImageStore',
                         source.type_url)
        unpacked = ctrd_transfer_types_pb2.ImageStore()
        source.Unpack(unpacked)
        self.assertEqual('harbor.example/p/app:v1', unpacked.name)

    def test_the_destination_is_the_registry_the_name_points_at(self):
        call, _answer = self._push()
        destination = call.args[0].destination

        self.assertEqual('type.googleapis.com/'
                         'containerd.types.transfer.OCIRegistry',
                         destination.type_url)
        unpacked = ctrd_transfer_types_pb2.OCIRegistry()
        destination.Unpack(unpacked)
        self.assertEqual('harbor.example/p/app:v1', unpacked.reference)

    def test_the_credential_travels_as_a_header(self):
        """An auth stream would be a second service for what Basic says."""
        call, _answer = self._push(username='robot$p+r', password='s3cret')
        destination = ctrd_transfer_types_pb2.OCIRegistry()
        call.args[0].destination.Unpack(destination)

        expected = base64.b64encode(b'robot$p+r:s3cret').decode()
        self.assertEqual('Basic %s' % expected,
                         destination.resolver.headers['Authorization'])

    def test_no_credential_means_no_header_rather_than_an_empty_one(self):
        call, _answer = self._push()
        destination = ctrd_transfer_types_pb2.OCIRegistry()
        call.args[0].destination.Unpack(destination)

        self.assertEqual({}, dict(destination.resolver.headers))

    def test_an_insecure_registry_is_reached_over_http(self):
        call, _answer = self._push(insecure=True)
        destination = ctrd_transfer_types_pb2.OCIRegistry()
        call.args[0].destination.Unpack(destination)

        self.assertEqual('http', destination.resolver.default_scheme)

    def test_the_namespace_header_is_sent(self):
        call, _answer = self._push()

        self.assertIn(('containerd-namespace', 'k8s.io'),
                      list(call.kwargs['metadata']))
