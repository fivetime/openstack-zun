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

"""Making an image out of a container, on containerd.

The CRI cannot do this: it runs containers and pulls images and has no
call that turns one into the other. containerd can, through three
services the CRI is a view over -- diff, content and images -- so this
reaches past it the same way pause and resume already do.

What a commit actually is: the files the container wrote, as one more
layer on top of the image it started from. containerd's diff service
computes that layer, which is the part worth delegating -- a deletion
inside a container is a whiteout in the layer, and hand-rolling that
from an overlay upper directory is how a committed image quietly comes
back with files the tenant deleted.
"""

import hashlib
import json

from oslo_log import log as logging
from oslo_utils import uuidutils

from zun.common import exception
from zun.common.i18n import _
from zun.criapi import imaging_pb2
from zun.criapi import snapshots_pb2

LOG = logging.getLogger(__name__)

#: The layer the diff service is asked for. Compressed, because that is
#: what every registry and every runtime expects to find in a manifest.
LAYER_MEDIA_TYPE = 'application/vnd.oci.image.layer.v1.tar+gzip'
#: containerd records the uncompressed digest here, and an image config
#: names layers by that digest rather than by the compressed one. Reading
#: it back beats decompressing the layer again to find out.
UNCOMPRESSED = 'containerd.io/uncompressed'
#: Where the base image's blobs already are. Kept on the committed image
#: because the push happens in a separate call that never sees the
#: container, and without it every layer travels again.
SOURCE_LABEL = 'zun.openstack.org/committed-from'

_MANIFEST_TYPES = ('application/vnd.oci.image.manifest.v1+json',
                   'application/vnd.docker.distribution.manifest.v2+json')
_INDEX_TYPES = ('application/vnd.oci.image.index.v1+json',
                'application/vnd.docker.distribution.manifest.list.v2+json')


def digest_of(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


class Committer(object):
    """One commit, against one node's containerd."""

    def __init__(self, driver, snapshotter, namespace):
        self.driver = driver
        self.snapshotter = snapshotter
        self.ns = namespace

    # ------------------------------------------------------------- blobs

    def read_blob(self, digest):
        chunks = []
        for response in self.driver.content_stub.Read(
                imaging_pb2.ReadContentRequest(digest=digest),
                metadata=self.ns):
            chunks.append(response.data)
        return b''.join(chunks)

    def write_blob(self, data, labels=None):
        """Put one blob in the content store, and say what it is.

        The labels are not decoration: containerd collects content that
        nothing refers to, and a manifest that does not name its config
        and its layers is a manifest whose parts can be collected out
        from under it.
        """
        digest = digest_of(data)
        ref = 'zun-commit-%s' % uuidutils.generate_uuid()

        def requests():
            yield imaging_pb2.WriteContentRequest(
                action=imaging_pb2.WRITE, ref=ref, total=len(data),
                expected=digest, offset=0, data=data, labels=labels or {})
            yield imaging_pb2.WriteContentRequest(
                action=imaging_pb2.COMMIT, ref=ref, total=len(data),
                expected=digest, offset=len(data), labels=labels or {})

        for _response in self.driver.content_stub.Write(requests(),
                                                        metadata=self.ns):
            pass
        return {'mediaType': None, 'digest': digest, 'size': len(data)}

    # ------------------------------------------------------------- layer

    def diff_layer(self, container_id):
        """The layer holding everything this container wrote.

        Asked of containerd rather than assembled here: the difference
        between two mounts is exactly what its diff service is for, and
        it is the only thing that gets whiteouts right.
        """
        info = self.driver.snapshot_stub.Stat(
            snapshots_pb2.StatSnapshotRequest(
                snapshotter=self.snapshotter, key=container_id),
            metadata=self.ns).info
        view = 'zun-commit-%s' % uuidutils.generate_uuid()
        lower = self.driver.snapshot_stub.View(
            snapshots_pb2.ViewSnapshotRequest(
                snapshotter=self.snapshotter, key=view,
                parent=info.parent), metadata=self.ns).mounts
        try:
            upper = self.driver.snapshot_stub.Mounts(
                snapshots_pb2.MountsRequest(
                    snapshotter=self.snapshotter, key=container_id),
                metadata=self.ns).mounts
            response = self.driver.diff_stub.Diff(
                imaging_pb2.DiffRequest(
                    left=[_mount(m) for m in lower],
                    right=[_mount(m) for m in upper],
                    media_type=LAYER_MEDIA_TYPE,
                    ref='zun-commit-%s' % uuidutils.generate_uuid()),
                metadata=self.ns)
        finally:
            try:
                self.driver.snapshot_stub.Remove(
                    snapshots_pb2.RemoveSnapshotRequest(
                        snapshotter=self.snapshotter, key=view),
                    metadata=self.ns)
            except Exception as exc:                        # noqa: BLE001
                LOG.warning('Could not remove the temporary snapshot '
                            '%(key)s: %(err)s', {'key': view, 'err': exc})
        return response.diff

    # ---------------------------------------------------------- assembly

    def base_manifest(self, image_name):
        """The manifest of the image a container was started from.

        An image may be stored as an index over several platforms; a
        commit is of one container on one machine, so the manifest for
        this node's platform is the one to build on.
        """
        target = self.driver.ctrd_image_stub.Get(
            imaging_pb2.GetImageRequest(name=image_name),
            metadata=self.ns).image.target
        if target.media_type in _INDEX_TYPES:
            index = json.loads(self.read_blob(target.digest))
            for entry in index.get('manifests', []):
                platform = entry.get('platform') or {}
                if (platform.get('architecture') == 'amd64'
                        and platform.get('os') == 'linux'):
                    return entry['mediaType'], json.loads(
                        self.read_blob(entry['digest']))
            raise exception.ZunException(_(
                'Image %s has no manifest for this platform') % image_name)
        if target.media_type not in _MANIFEST_TYPES:
            raise exception.ZunException(_(
                'Image %(image)s is stored as %(type)s, which a commit '
                'cannot build on')
                % {'image': image_name, 'type': target.media_type})
        return target.media_type, json.loads(self.read_blob(target.digest))

    def commit(self, container, name, source=None):
        """Build the image and record it, returning its manifest digest."""
        layer = self.diff_layer(container.container_id)
        diff_id = (layer.annotations or {}).get(UNCOMPRESSED)
        if not diff_id:
            raise exception.ZunException(_(
                'containerd did not report the uncompressed digest of the '
                'committed layer, so the image config cannot name it'))

        media_type, manifest = self.base_manifest(container.image)
        config = json.loads(self.read_blob(manifest['config']['digest']))
        config.setdefault('rootfs', {}).setdefault('diff_ids', [])
        config['rootfs']['diff_ids'].append(diff_id)
        config.setdefault('history', []).append({
            'created_by': 'zun commit %s' % container.uuid,
            'comment': 'committed from container %s' % container.uuid})
        config_blob = json.dumps(config, separators=(',', ':')).encode()
        config_desc = self.write_blob(config_blob)

        manifest['config'] = {
            'mediaType': manifest['config']['mediaType'],
            'digest': config_desc['digest'], 'size': config_desc['size']}
        manifest.setdefault('layers', []).append({
            'mediaType': layer.media_type, 'digest': layer.digest,
            'size': layer.size})
        manifest_blob = json.dumps(manifest, separators=(',', ':')).encode()

        # What keeps the parts alive. containerd walks these labels to
        # decide what is still referenced; without them the config and
        # the layers are unreferenced blobs the moment they are written.
        labels = {'containerd.io/gc.ref.content.config':
                  config_desc['digest']}
        for index, entry in enumerate(manifest['layers']):
            labels['containerd.io/gc.ref.content.l.%d' % index] = \
                entry['digest']
        manifest_desc = self.write_blob(manifest_blob, labels=labels)

        self.driver.ctrd_image_stub.Create(
            imaging_pb2.CreateImageRequest(
                image=imaging_pb2.Image(
                    name=name,
                    labels={SOURCE_LABEL: source} if source else {},
                    target=imaging_pb2.Descriptor(
                        media_type=media_type,
                        digest=manifest_desc['digest'],
                        size=manifest_desc['size']))),
            metadata=self.ns)
        LOG.info('Committed container %(container)s as %(name)s (%(digest)s)',
                 {'container': container.uuid, 'name': name,
                  'digest': manifest_desc['digest']})
        return manifest_desc['digest']


def _mount(mount):
    """A mount as the diff service takes it, from what snapshots returned."""
    return imaging_pb2.Mount(type=mount.type, source=mount.source,
                             target=mount.target,
                             options=list(mount.options))
