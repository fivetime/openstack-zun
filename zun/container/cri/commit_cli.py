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

import json
import sys

import grpc

from zun.container.cri import commit as cri_commit
from zun.container.cri import registry as cri_registry
from zun.criapi import ctrd_content_pb2_grpc
from zun.criapi import ctrd_diff_pb2_grpc
from zun.criapi import ctrd_images_pb2
from zun.criapi import ctrd_images_pb2_grpc
from zun.criapi import snapshots_pb2_grpc


class _Stubs(object):
    """What Committer reaches for, without importing the driver."""

    def __init__(self, address):
        channel = grpc.insecure_channel(address)
        self.snapshot_stub = snapshots_pb2_grpc.SnapshotsStub(channel)
        self.diff_stub = ctrd_diff_pb2_grpc.DiffStub(channel)
        self.content_stub = ctrd_content_pb2_grpc.ContentStub(channel)
        self.ctrd_image_stub = ctrd_images_pb2_grpc.ImagesStub(channel)


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


def do_push(request):
    committer = _committer(request)
    image = committer.driver.ctrd_image_stub.Get(
        ctrd_images_pb2.GetImageRequest(name=request['name']),
        metadata=committer.ns).image
    manifest = json.loads(committer.read_blob(image.target.digest))
    source = (image.labels or {}).get(cri_commit.SOURCE_LABEL) or None

    client = cri_registry.Registry(
        request['host'], request['repository'],
        username=request.get('username'), password=request.get('password'),
        verify=not request.get('insecure'), timeout=request['timeout'])
    sent = 0
    for blob in list(manifest.get('layers', [])) + [manifest['config']]:
        if client.has_blob(blob['digest']):
            continue
        if source and client.mount_blob(blob['digest'], source):
            continue
        client.put_blob(blob['digest'], committer.read_blob(blob['digest']))
        sent += 1
    client.put_manifest(request['tag'], image.target.media_type,
                        committer.read_blob(image.target.digest))
    return {'name': request['name'], 'blobs_sent': sent}


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
