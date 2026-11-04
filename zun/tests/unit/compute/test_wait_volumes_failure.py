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

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.compute import manager
import zun.conf
from zun.container import driver as container_driver
from zun import objects
from zun.tests.unit.db import base
from zun.tests.unit.db import utils


class WaitForVolumesFailureTest(base.DbTestCase):
    """A volume nothing here can serve fails the create, and says so.

    It used to leave the record in Creating for good: the exception ended
    a background task nobody watches, and nothing marked the record.
    """

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        # What the deployment had: a list without the provider asked for.
        zun.conf.CONF.set_override('driver_list', ['cinder', 'local'],
                                   group='volume')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.driver = container_driver.BaseDriver()
        p = mock.patch.object(self.manager, '_get_driver',
                              return_value=self.driver)
        p.start()
        self.addCleanup(p.stop)
        self.container = objects.Container(
            self.context, **utils.get_test_container())
        self.container.status = consts.CREATING
        self.container.host = 'fake-host'
        p = mock.patch.object(objects.Container, 'save')
        self.save = p.start()
        self.addCleanup(p.stop)

    def _volmap(self, provider='emptydir', auto_remove=True):
        return mock.Mock(volume_provider=provider, auto_remove=auto_remove)

    def _wait(self, *volmaps):
        self.manager._wait_for_volumes_available(
            self.context, {self.container.uuid: list(volmaps)},
            self.container, timeout=1)

    def test_unsupported_provider_fails_the_create(self):
        self.assertRaises(exception.Conflict, self._wait, self._volmap())

        self.assertEqual(consts.ERROR, self.container.status)
        self.assertIn("'emptydir' is not supported",
                      self.container.status_reason)
        self.save.assert_called()

    def test_supported_provider_is_left_alone(self):
        self._wait(self._volmap(provider='local'))

        self.assertEqual(consts.CREATING, self.container.status)

    def test_unexpected_error_does_not_reach_the_tenant(self):
        # Whatever a library raised may name what a tenant has no business
        # reading; the log has it.
        with mock.patch.object(
                self.driver, 'is_volume_available',
                side_effect=RuntimeError('http://10.0.0.1:8776 refused')):
            self.assertRaises(exception.Conflict, self._wait,
                              self._volmap(provider='local'))

        self.assertEqual(consts.ERROR, self.container.status)
        self.assertNotIn('10.0.0.1', self.container.status_reason)

    def test_no_volumes(self):
        self._wait()

        self.assertEqual(consts.CREATING, self.container.status)


class AttachCapsuleVolumesFailureTest(base.DbTestCase):
    """A capsule whose volume is refused at attach is what goes to Error.

    The failure used to be recorded on the member being attached. The capsule
    is what a tenant reads and what the create is waiting on, and it stayed
    in Creating for good.
    """

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        # Left false, the NFS driver refuses to mount: the real refusal.
        zun.conf.CONF.set_override('driver_list', ['nfs'], group='volume')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        p = mock.patch.object(self.manager, '_get_driver',
                              return_value=container_driver.BaseDriver())
        p.start()
        self.addCleanup(p.stop)
        for cls in (objects.Capsule, objects.CapsuleContainer):
            p = mock.patch.object(cls, 'save')
            p.start()
            self.addCleanup(p.stop)
        self.member = objects.CapsuleContainer(
            self.context, **utils.get_test_container(
                uuid='5d8c1a2b-3f4e-4a6b-8c9d-0e1f2a3b4c5d'))
        self.member.status = consts.CREATING
        self.capsule = objects.Capsule(
            self.context, **utils.get_test_container())
        self.capsule.status = consts.CREATING
        self.capsule.containers = [self.member]
        self.capsule.init_containers = []
        self.volmap = mock.Mock(volume_provider='nfs', connection_info=None)
        self.volmap.volume.contents = '{"export": "nfs.invalid:/x"}'

    def test_refused_volume_fails_the_capsule(self):
        self.assertRaises(
            exception.ZunException, self.manager._attach_volumes,
            self.context, self.capsule, {self.member.uuid: [self.volmap]})

        self.assertEqual(consts.ERROR, self.capsule.status)
        self.assertIn('dedicated to capsules', self.capsule.status_reason)

    def test_unexpected_error_does_not_reach_the_tenant(self):
        with mock.patch.object(
                self.manager, '_attach_volume',
                side_effect=RuntimeError('http://10.0.0.1:8776 refused')):
            self.assertRaises(
                RuntimeError, self.manager._attach_volumes,
                self.context, self.capsule, {self.member.uuid: [self.volmap]})

        self.assertEqual(consts.ERROR, self.capsule.status)
        self.assertNotIn('10.0.0.1', self.capsule.status_reason)

    def test_failed_command_does_not_reach_the_tenant(self):
        # What a mount of a share leaves in the exception: the command line,
        # the node's path, and on some backends a pool or monitor address.
        with mock.patch.object(
                self.manager, '_attach_volume',
                side_effect=exception.CommandError(
                    cmd='mount -t nfs 10.0.0.1:/x /var/lib/zun/mnt/abc',
                    error='mount.nfs: access denied')):
            self.assertRaises(
                exception.CommandError, self.manager._attach_volumes,
                self.context, self.capsule, {self.member.uuid: [self.volmap]})

        for record in (self.capsule, self.member):
            self.assertEqual(consts.ERROR, record.status)
            self.assertNotIn('10.0.0.1', record.status_reason)
            self.assertNotIn('/var/lib/zun', record.status_reason)

    def test_failure_built_on_a_command_does_not_reach_the_tenant(self):
        with mock.patch.object(
                self.manager, '_attach_volume',
                side_effect=exception.MountException(
                    'Devpath: /dev/rbd0, Error: mon 10.0.0.1 refused')):
            self.assertRaises(
                exception.MountException, self.manager._attach_volumes,
                self.context, self.capsule, {self.member.uuid: [self.volmap]})

        self.assertNotIn('10.0.0.1', self.capsule.status_reason)
        self.assertNotIn('/dev/rbd0', self.member.status_reason)

    def test_refused_volume_tells_the_member_too(self):
        self.assertRaises(
            exception.ZunException, self.manager._attach_volumes,
            self.context, self.capsule, {self.member.uuid: [self.volmap]})

        self.assertIn('dedicated to capsules', self.member.status_reason)

    def test_manila_refusal_does_not_reach_the_tenant(self):
        # Manila's words are not ours to repeat, though we raised them: what
        # it said about a share may be more than the tenant may read.
        with mock.patch.object(
                self.manager, '_attach_volume',
                side_effect=exception.ZunException(
                    'manila refused access_list on share kzvol-fake: 404 '
                    '{"itemNotFound": {}}')):
            self.assertRaises(
                exception.ZunException, self.manager._attach_volumes,
                self.context, self.capsule, {self.member.uuid: [self.volmap]})

        for record in (self.capsule, self.member):
            self.assertNotIn('manila', record.status_reason)


class NfsMountFailureTest(base.DbTestCase):
    """A real mount failure, through the real driver."""

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        zun.conf.CONF.set_override('driver_list', ['nfs'], group='volume')
        zun.conf.CONF.set_override('host_dedicated_to_capsules', True,
                                   group='volume')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        p = mock.patch.object(self.manager, '_get_driver',
                              return_value=container_driver.BaseDriver())
        p.start()
        self.addCleanup(p.stop)
        for cls in (objects.Capsule, objects.CapsuleContainer):
            p = mock.patch.object(cls, 'save')
            p.start()
            self.addCleanup(p.stop)
        self.member = objects.CapsuleContainer(
            self.context, **utils.get_test_container(
                uuid='5d8c1a2b-3f4e-4a6b-8c9d-0e1f2a3b4c5d'))
        self.capsule = objects.Capsule(
            self.context, **utils.get_test_container())
        self.capsule.containers = [self.member]
        self.capsule.init_containers = []
        self.volmap = mock.Mock(volume_provider='nfs', connection_info=None)
        self.volmap.volume.uuid = 'b7a3f1c2-6d4e-4f8a-9b1c-2d3e4f5a6b7c'
        self.volmap.volume.contents = '{"export": "nfs.invalid:/x"}'

    def test_reason_says_the_mount_failed_and_nothing_else(self):
        with mock.patch('zun.common.utils.execute',
                        side_effect=exception.CommandError(
                            cmd='timeout 60 mount nfs.invalid:/x '
                                '/var/lib/zun/mnt/abc',
                            error='mount.nfs: Failed to resolve server')), \
                mock.patch('oslo_utils.fileutils.ensure_tree'), \
                mock.patch('os.path.ismount', return_value=False):
            self.assertRaises(
                exception.VolumeNotPrepared, self.manager._attach_volumes,
                self.context, self.capsule, {self.member.uuid: [self.volmap]})

        for record in (self.capsule, self.member):
            self.assertEqual(consts.ERROR, record.status)
            self.assertIn('NFS export', record.status_reason)
            self.assertNotIn('/var/lib/zun', record.status_reason)
            self.assertNotIn('mount.nfs', record.status_reason)


class ReleaseAllocationOnFailureTest(base.DbTestCase):
    """A record that has lost its host holds no claim on that host.

    Wait, attach and the disk check all come before the claim's own error
    handling, so their failures kept the allocation. The periodic sweep only
    collects it later, and does not run at all on a host shared with nova.
    """

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.released = self.manager.reportclient.\
            delete_allocation_for_container
        p = mock.patch.object(self.manager, '_detach_volumes')
        p.start()
        self.addCleanup(p.stop)
        for cls in (objects.Capsule, objects.Container,
                    objects.CapsuleContainer):
            p = mock.patch.object(cls, 'save')
            p.start()
            self.addCleanup(p.stop)
        self.capsule = objects.Capsule(
            self.context, **utils.get_test_container())
        self.capsule.auto_remove = False

    def test_attach_failure_releases_the_capsule_allocation(self):
        member = objects.CapsuleContainer(
            self.context, **utils.get_test_container(
                uuid='5d8c1a2b-3f4e-4a6b-8c9d-0e1f2a3b4c5d'))
        member.auto_remove = False
        self.capsule.containers = [member]
        self.capsule.init_containers = []
        with mock.patch.object(self.manager, '_attach_volume',
                               side_effect=RuntimeError('boom')):
            self.assertRaises(
                RuntimeError, self.manager._attach_volumes, self.context,
                self.capsule, {member.uuid: [mock.Mock(connection_info=None)]})

        # Placement knows the capsule; a member has no allocation of its own.
        self.released.assert_called_once_with(self.context, self.capsule.uuid)

    def test_keeping_the_host_keeps_the_allocation(self):
        self.manager._fail_container(self.context, self.capsule, 'x',
                                     unset_host=False)

        self.released.assert_not_called()

    def test_failed_release_does_not_hide_the_failure(self):
        self.released.side_effect = RuntimeError('placement is down')

        self.manager._fail_container(self.context, self.capsule, 'the reason',
                                     unset_host=True)

        self.assertEqual(consts.ERROR, self.capsule.status)
        self.assertEqual('the reason', self.capsule.status_reason)
        self.assertIsNone(self.capsule.host)

    def test_auto_remove_container_releases_too(self):
        container = objects.Container(
            self.context, **utils.get_test_container())
        container.auto_remove = True

        self.manager._fail_container(self.context, container, 'x',
                                     unset_host=True)

        self.assertEqual(consts.DELETED, container.status)
        self.released.assert_called_once_with(self.context, container.uuid)
