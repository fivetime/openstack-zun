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

"""A commit, run in a process of its own.

Not for isolation and not for parallelism: for the event loop. Making an
image reads a layer back out of containerd's content store, and that is
a server-streaming call, which under eventlet's monkey patching never
returns -- the gRPC core signals completion on a native thread and the
waiter it has to wake is a green one that only the hub can run.
Measured on this stack: unary calls are fine, a streaming read hangs so
hard that `eventlet.Timeout` cannot interrupt it, and the compute
service stopped answering its heartbeat until it was restarted.

So the work happens where nothing is patched. The parameters arrive as
JSON on stdin -- a registry password on a command line is a password in
`ps` -- and the answer goes back as JSON on stdout.
"""

import base64
import json
import sys

from google.protobuf import any_pb2
import grpc

from zun.container.cri import commit as cri_commit
from zun.criapi import ctrd_content_pb2_grpc
from zun.criapi import ctrd_diff_pb2_grpc
from zun.criapi import ctrd_images_pb2_grpc
from zun.criapi import ctrd_transfer_pb2
from zun.criapi import ctrd_transfer_pb2_grpc
from zun.criapi import ctrd_transfer_types_pb2
from zun.criapi import snapshots_pb2_grpc


class _Stubs(object):
    """What Committer reaches for, without importing the driver."""

    def __init__(self, address):
        channel = grpc.insecure_channel(address)
        self.snapshot_stub = snapshots_pb2_grpc.SnapshotsStub(channel)
        self.diff_stub = ctrd_diff_pb2_grpc.DiffStub(channel)
        self.content_stub = ctrd_content_pb2_grpc.ContentStub(channel)
        self.ctrd_image_stub = ctrd_images_pb2_grpc.ImagesStub(channel)
        self.transfer_stub = ctrd_transfer_pb2_grpc.TransferStub(channel)


class _Container(object):
    def __init__(self, request):
        self.uuid = request['uuid']
        self.container_id = request['container_id']
        self.image = request.get('image')


def _committer(request):
    namespace = (('containerd-namespace', request['namespace']),)
    return cri_commit.Committer(_Stubs(request['address']),
                                request['snapshotter'], namespace)


def do_commit(request):
    digest = _committer(request).commit(_Container(request), request['name'],
                                        source=request.get('source'))
    return {'digest': digest, 'name': request['name']}


def _packed(message):
    """A message in an Any, under the type url containerd looks it up by.

    Not Any.Pack: that writes `type.googleapis.com/<name>`, and
    containerd's typeurl registers and resolves by the bare proto name.
    With the prefix the lookup misses, the message comes back as itself
    rather than as the thing it means, and the transfer is refused as a
    combination that is not implemented -- which reads as a missing
    feature rather than a mistyped envelope.
    """
    return any_pb2.Any(
        type_url=message.DESCRIPTOR.full_name,
        value=message.SerializeToString())


def do_push(request):
    """Hand the image to containerd and let it do the pushing.

    Not a registry client of our own: containerd's transfer service
    already implements the protocol, and every pull on every node
    exercises it. The one written here instead had four separate
    authentication bugs, each of which arrived as the same 403.

    The credential goes in as a header rather than through an auth
    stream, which would be a second service to implement for something
    Basic already says.
    """
    stubs = _Stubs(request['address'])
    namespace = (('containerd-namespace', request['namespace']),)

    source = _packed(ctrd_transfer_types_pb2.ImageStore(
        name=request['name']))

    headers = {}
    if request.get('username'):
        credential = '%s:%s' % (request['username'], request.get('password'))
        headers['Authorization'] = 'Basic %s' % base64.b64encode(
            credential.encode()).decode()
    resolver = ctrd_transfer_types_pb2.RegistryResolver(
        headers=headers,
        default_scheme='http' if request.get('insecure') else 'https')
    destination = _packed(ctrd_transfer_types_pb2.OCIRegistry(
        reference=request['name'], resolver=resolver))

    stubs.transfer_stub.Transfer(
        ctrd_transfer_pb2.TransferRequest(source=source,
                                          destination=destination),
        metadata=namespace, timeout=request['timeout'])
    return {'name': request['name']}


_ACTIONS = {'commit': do_commit, 'push': do_push}


def main():
    request = json.loads(sys.stdin.read())
    action = _ACTIONS.get(request.get('action'))
    if action is None:
        json.dump({'error': 'unknown action %r' % request.get('action')},
                  sys.stdout)
        return 2
    try:
        json.dump(action(request), sys.stdout)
    except Exception as exc:                                # noqa: BLE001
        # Reported rather than raised: the caller reads stdout, and a
        # traceback on stderr tells it only that something went wrong.
        json.dump({'error': '%s: %s' % (type(exc).__name__, exc)},
                  sys.stdout)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
