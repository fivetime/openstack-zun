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

The layer itself is containerd's to compute -- a deletion inside a
container is a whiteout in the layer, and that is the part worth
delegating. What is assembled here is the config and the manifest that
name it, and the labels that keep the pieces from being collected.
"""

import json
from unittest import mock

from zun.container.cri import commit as cri_commit
from zun.criapi import ctrd_content_pb2
from zun.criapi import ctrd_diff_pb2
from zun.criapi import ctrd_images_pb2
from zun.tests import base


def _descriptor(digest='sha256:layer', size=10, uncompressed='sha256:diff'):
    return ctrd_images_pb2.Descriptor(
        media_type=cri_commit.LAYER_MEDIA_TYPE, digest=digest, size=size,
        annotations={cri_commit.UNCOMPRESSED: uncompressed}
        if uncompressed else {})


_MANIFEST = {
    'schemaVersion': 2,
    'mediaType': 'application/vnd.oci.image.manifest.v1+json',
    'config': {'mediaType': 'application/vnd.oci.image.config.v1+json',
               'digest': 'sha256:cfg', 'size': 3},
    'layers': [{'mediaType': cri_commit.LAYER_MEDIA_TYPE,
                'digest': 'sha256:base', 'size': 5}],
}
_CONFIG = {'rootfs': {'type': 'layers', 'diff_ids': ['sha256:basediff']}}


class CommitTest(base.TestCase):

    def setUp(self):
        super(CommitTest, self).setUp()
        self.driver = mock.Mock()
        self.driver.ctrd_image_stub.Get.return_value.image.target = \
            ctrd_images_pb2.Descriptor(
                media_type='application/vnd.oci.image.manifest.v1+json',
                digest='sha256:manifest', size=7)
        self.committer = cri_commit.Committer(self.driver, 'overlayfs', ())
        self.container = mock.Mock(uuid='u-1', container_id='c-1',
                                   image='harbor/proj/app:v1')
        self.written = []

        def blobs(digest):
            if digest == 'sha256:manifest':
                return json.dumps(_MANIFEST).encode()
            if digest == 'sha256:cfg':
                return json.dumps(_CONFIG).encode()
            raise AssertionError('unexpected blob %s' % digest)

        self.committer.read_blob = blobs

        def write(data, labels=None):
            self.written.append((data, labels or {}))
            return {'digest': 'sha256:w%d' % len(self.written),
                    'size': len(data)}

        self.committer.write_blob = write

    def _commit(self, layer=None):
        with mock.patch.object(self.committer, 'diff_layer',
                               return_value=layer or _descriptor()):
            return self.committer.commit(self.container, 'repo:tag',
                                         source='proj/app')

    def test_the_new_layer_is_named_in_the_config_by_its_diff_id(self):
        self._commit()
        config = json.loads(self.written[0][0])

        self.assertEqual(['sha256:basediff', 'sha256:diff'],
                         config['rootfs']['diff_ids'])

    def test_the_new_layer_is_appended_to_the_manifest(self):
        self._commit()
        manifest = json.loads(self.written[1][0])

        self.assertEqual(['sha256:base', 'sha256:layer'],
                         [entry['digest'] for entry in manifest['layers']])

    def test_the_manifest_points_at_the_new_config(self):
        self._commit()
        manifest = json.loads(self.written[1][0])

        self.assertEqual('sha256:w1', manifest['config']['digest'])

    def test_the_manifest_carries_the_labels_that_keep_its_parts(self):
        """Unreferenced blobs are collected out from under the manifest."""
        self._commit()
        labels = self.written[1][1]

        self.assertEqual('sha256:w1',
                         labels['containerd.io/gc.ref.content.config'])
        self.assertEqual('sha256:base',
                         labels['containerd.io/gc.ref.content.l.0'])
        self.assertEqual('sha256:layer',
                         labels['containerd.io/gc.ref.content.l.1'])

    def test_the_image_records_where_its_blobs_came_from(self):
        self._commit()
        image = self.driver.ctrd_image_stub.Create.call_args.args[0].image

        self.assertEqual('proj/app', image.labels[cri_commit.SOURCE_LABEL])

    def test_a_layer_without_the_annotation_still_gets_its_diff_id(self):
        """Read back rather than guessed: see DiffIdTest."""
        with mock.patch.object(self.committer, 'diff_id',
                               return_value='sha256:computed') as read:
            self._commit(_descriptor(uncompressed=None))

        read.assert_called_once()
        config = json.loads(self.written[0][0])
        self.assertEqual(['sha256:basediff', 'sha256:computed'],
                         config['rootfs']['diff_ids'])


class DiffLayerTest(base.TestCase):

    def setUp(self):
        super(DiffLayerTest, self).setUp()
        self.driver = mock.Mock()
        self.driver.snapshot_stub.Stat.return_value.info.parent = 'parent-1'
        mount = mock.Mock(type='overlay', source='overlay', target='',
                          options=['upperdir=/up'])
        self.driver.snapshot_stub.View.return_value.mounts = [mount]
        self.driver.snapshot_stub.Mounts.return_value.mounts = [mount]
        self.committer = cri_commit.Committer(self.driver, 'overlayfs', ())

    def test_the_diff_is_taken_against_the_image_the_container_started_from(
            self):
        self.committer.diff_layer('c-1')
        view = self.driver.snapshot_stub.View.call_args.args[0]

        self.assertEqual('parent-1', view.parent)

    def test_the_temporary_view_is_removed(self):
        self.committer.diff_layer('c-1')

        self.driver.snapshot_stub.Remove.assert_called_once()

    def test_it_is_removed_even_when_the_diff_fails(self):
        self.driver.diff_stub.Diff.side_effect = RuntimeError('no')

        self.assertRaises(RuntimeError, self.committer.diff_layer, 'c-1')
        self.driver.snapshot_stub.Remove.assert_called_once()


class StubsDoNotShadowEachOtherTest(base.TestCase):
    """Two image services, and only one of them pulls.

    The CRI's image service pulls, lists and removes; containerd's own
    image store is where a commit records what it made. Giving them the
    same attribute cost every container on the node -- pulling went
    looking for a call the other stub has not got -- and no test saw it,
    because a test that mocks the driver mocks the collision away too.
    """

    def _driver(self):
        from zun.container.cri import driver as cri_driver
        with mock.patch.object(cri_driver.grpc, 'insecure_channel'):
            with mock.patch.object(cri_driver.img_driver,
                                   'load_image_driver'):
                with mock.patch.object(cri_driver.CONF, 'image_driver_list',
                                       []):
                    return cri_driver.CriDriver()

    def test_the_cri_image_service_still_pulls(self):
        self.assertTrue(hasattr(self._driver().image_stub, 'PullImage'))

    def test_containerd_s_image_store_is_reachable_separately(self):
        driver = self._driver()

        self.assertTrue(hasattr(driver.ctrd_image_stub, 'Create'))
        self.assertFalse(hasattr(driver.ctrd_image_stub, 'PullImage'))


class DiffIdTest(base.TestCase):
    """The config names a layer by its uncompressed digest.

    containerd annotates it when it has one. When it does not, the
    digest is read back out of the layer rather than guessed -- a
    config naming the wrong one produces an image that pulls and then
    will not unpack.
    """

    def setUp(self):
        super(DiffIdTest, self).setUp()
        self.driver = mock.Mock()
        self.committer = cri_commit.Committer(self.driver, 'overlayfs', ())

    def test_the_annotation_is_used_when_it_is_there(self):
        self.driver.content_stub.Read.return_value = []

        self.assertEqual('sha256:diff',
                         self.committer.diff_id(_descriptor()))
        self.driver.content_stub.Read.assert_not_called()

    def test_otherwise_it_is_computed_from_the_layer(self):
        import gzip
        import hashlib
        raw = b'a tar would be here'
        packed = gzip.compress(raw)
        self.driver.content_stub.Read.return_value = [
            mock.Mock(data=packed[:5]), mock.Mock(data=packed[5:])]

        self.assertEqual('sha256:' + hashlib.sha256(raw).hexdigest(),
                         self.committer.diff_id(_descriptor(
                             uncompressed=None)))


class CommitRunsInItsOwnProcessTest(base.TestCase):
    """A streaming read never returns under eventlet's monkey patching.

    The gRPC core signals completion on a native thread and the waiter
    it must wake is a green one only the hub can run. Measured on this
    stack: unary calls are fine, a streaming read hangs so hard that
    eventlet.Timeout cannot interrupt it, and the compute service
    stopped answering its heartbeat until restarted. So the work
    happens where nothing is patched.
    """

    def _driver(self):
        from zun.container.cri import driver as cri_driver
        return cri_driver, cri_driver.CriDriver.__new__(cri_driver.CriDriver)

    def test_commit_goes_out_to_the_helper(self):
        cri_driver, driver = self._driver()
        container = mock.Mock(uuid='u-1', container_id='c-1',
                              image='harbor/proj/app:v1')
        with mock.patch.object(cri_driver.utils, 'execute',
                               return_value=('{"digest": "sha256:x"}', '')
                               ) as ran:
            name = driver.commit({}, container, 'repo', 'tag')

        self.assertEqual('repo:tag', name)
        self.assertIn(driver._CLI, ran.call_args.args)

    def test_the_secret_travels_on_stdin_not_the_command_line(self):
        """A registry password on a command line is a password in ps."""
        cri_driver, driver = self._driver()
        registry = mock.Mock(username='robot', password='s3cret')
        with mock.patch.object(cri_driver.utils, 'execute',
                               return_value=('{"name": "n"}', '')) as ran:
            driver.push_image({}, 'harbor.tue.jp/proj/app', 'v1', registry,
                              None)

        self.assertNotIn('s3cret', ' '.join(str(a)
                                            for a in ran.call_args.args))
        self.assertIn('s3cret', ran.call_args.kwargs['process_input'])

    def test_a_refused_push_is_raised_not_logged_away(self):
        cri_driver, driver = self._driver()
        with mock.patch.object(cri_driver.utils, 'execute',
                               return_value=('{"error": "no such repo"}',
                                             '')):
            error = self.assertRaises(
                Exception, driver.push_image, {},
                'harbor.tue.jp/proj/app', 'v1', None, None)

        self.assertIn('no such repo', str(error))

    def test_a_name_without_a_registry_is_refused_before_anything_runs(self):
        cri_driver, driver = self._driver()
        with mock.patch.object(cri_driver.utils, 'execute') as ran:
            self.assertRaises(Exception, driver.push_image, {}, 'app', 'v1',
                              None, None)

        ran.assert_not_called()
