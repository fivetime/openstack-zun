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

import abc
import functools
import os
import shutil

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils
from oslo_utils import fileutils
from stevedore import driver as stevedore_driver

from zun.common import exception
from zun.common.i18n import _
from zun.common import mount
from zun.common import utils
import zun.conf
from zun.volume import cinder_api
from zun.volume import cinder_workflow

LOG = logging.getLogger(__name__)

CONF = zun.conf.CONF


def driver(driver_name, *args, **kwargs):
    LOG.info("Loading volume driver '%s'", driver_name)
    volume_driver = stevedore_driver.DriverManager(
        "zun.volume.driver",
        driver_name,
        invoke_on_load=True,
        invoke_args=args,
        invoke_kwds=kwargs).driver
    if not isinstance(volume_driver, VolumeDriver):
        raise exception.ZunException(_("Invalid volume driver type"))
    return volume_driver


def validate_volume_provider(supported_providers):
    """Wraps a method to validate volume provider."""

    def decorator(function):
        @functools.wraps(function)
        def decorated_function(self, context, volume, **kwargs):
            provider = volume.volume_provider
            if provider not in supported_providers:
                msg = _("The volume provider '%s' is not supported") % provider
                raise exception.ZunException(msg)

            return function(self, context, volume, **kwargs)

        return decorated_function
    return decorator


class VolumeDriver(object, metaclass=abc.ABCMeta):
    """The base class that all Volume classes should inherit from."""

    def attach(self, *args, **kwargs):
        raise NotImplementedError()

    def detach(self, *args, **kwargs):
        raise NotImplementedError()

    def delete(self, *args, **kwargs):
        raise NotImplementedError()

    def bind_mount(self, *args, **kwargs):
        raise NotImplementedError()

    def is_volume_available(self, context, volmap):
        raise NotImplementedError()

    def is_volume_deleted(self, context, volmap):
        raise NotImplementedError()


class Local(VolumeDriver):

    supported_providers = ['local']

    @validate_volume_provider(supported_providers)
    def attach(self, context, volmap):
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        fileutils.ensure_tree(mountpoint)
        filename = '/'.join([mountpoint, volmap.volume.uuid])
        with open(filename, 'wb') as fd:
            content = utils.decode_file_data(volmap.contents)
            fd.write(content)

    @validate_volume_provider(supported_providers)
    def update_file(self, context, volmap, contents):
        """Rewrite the file this volume is, leaving the file itself in place.

        The file is bind mounted into the container, so the container follows
        it by inode: truncating and writing is seen immediately, while writing
        a new file and renaming it over this one would leave the container
        reading a file that no longer exists anywhere.
        """
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        filename = '/'.join([mountpoint, volmap.volume.uuid])
        with open(filename, 'wb') as fd:
            fd.write(utils.decode_file_data(contents))
            fd.flush()
            os.fsync(fd.fileno())

    def _remove_local_file(self, volmap):
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        shutil.rmtree(mountpoint)

    @validate_volume_provider(supported_providers)
    def detach(self, context, volmap):
        self._remove_local_file(volmap)

    @validate_volume_provider(supported_providers)
    def delete(self, context, volmap):
        self._remove_local_file(volmap)

    @validate_volume_provider(supported_providers)
    def bind_mount(self, context, volmap):
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        filename = '/'.join([mountpoint, volmap.volume.uuid])
        return filename, volmap.container_path

    def is_volume_available(self, context, volmap):
        return True, False

    def is_volume_deleted(self, context, volmap):
        return True, False


class EmptyDir(VolumeDriver):
    """A directory that lives and dies with the capsule.

    Kubernetes calls it emptyDir and nearly every workload assumes one: a
    sidecar writing where another reads, a cache directory, a socket two
    processes meet on. It is also what makes an image with a read-only root
    filesystem usable, which is most of the well-built ones.

    Every container of the capsule that mounts it gets the same directory --
    the sharing is the point, not a side effect -- and it goes when the capsule
    does.
    """

    supported_providers = ['emptydir']

    # An emptyDir is writable by whoever the containers run as, and a capsule
    # does not say who that is until its image does. A kubelet solves this the
    # same way: the directory is world-writable unless an fsGroup narrows it.
    # Anything stricter and a container running as a non-root user -- which is
    # every image worth running -- cannot write to its own scratch space.
    MODE = 0o777

    @validate_volume_provider(supported_providers)
    def attach(self, context, volmap):
        path = self._path(volmap)
        fileutils.ensure_tree(path)
        os.chmod(path, self.MODE)
        if self._medium(volmap) != 'Memory':
            # A directory on the node. Nothing here can enforce a size limit
            # on it, and the caller was told so rather than being given one
            # that does not hold.
            return

        # A capsule's containers each get their own mapping to the same
        # volume, so this runs once per container that mounts it. Mounting a
        # tmpfs over itself would fail the second time and take the capsule
        # with it.
        if os.path.ismount(path):
            return

        # tmpfs: what a caller asking for Memory wants -- speed, and content
        # that never reaches a disk. The size limit is real here because the
        # kernel enforces it.
        opts = ['mode=%o' % self.MODE]
        limit = self._size_limit(volmap)
        if limit:
            opts.append('size=%d' % limit)
        utils.execute('mount', '-t', 'tmpfs', '-o', ','.join(opts),
                      'tmpfs', path, run_as_root=True)

    def _detach(self, volmap):
        path = self._path(volmap)
        if self._medium(volmap) == 'Memory':
            try:
                utils.execute('umount', path, run_as_root=True)
            except Exception as e:
                # Already gone, or never mounted. Removing the directory is
                # still right; leaving it would leak one per capsule.
                LOG.debug("Could not unmount %(path)s: %(err)s",
                          {'path': path, 'err': e})
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)

    @validate_volume_provider(supported_providers)
    def detach(self, context, volmap):
        self._detach(volmap)

    @validate_volume_provider(supported_providers)
    def delete(self, context, volmap):
        self._detach(volmap)

    @validate_volume_provider(supported_providers)
    def bind_mount(self, context, volmap):
        return self._path(volmap), volmap.container_path

    def is_volume_available(self, context, volmap):
        return True, False

    def is_volume_deleted(self, context, volmap):
        return True, False

    @staticmethod
    def _path(volmap):
        return mount.get_mountpoint(volmap.volume.uuid)

    @staticmethod
    def _options(volmap):
        raw = volmap.volume.contents
        if not raw:
            return {}
        try:
            return jsonutils.loads(raw)
        except ValueError:
            return {}

    @classmethod
    def _medium(cls, volmap):
        return cls._options(volmap).get('medium') or ''

    @classmethod
    def _size_limit(cls, volmap):
        try:
            return int(cls._options(volmap).get('sizeLimit') or 0)
        except (TypeError, ValueError):
            return 0


class Cinder(VolumeDriver):

    supported_providers = [
        'cinder'
    ]

    @validate_volume_provider(supported_providers)
    def attach(self, context, volmap):
        cinder = cinder_workflow.CinderWorkflow(context)
        if volmap.connection_info:
            # this is a re-attach of the volume
            connection_info = jsonutils.loads(volmap.connection_info)
            device_info = cinder._connect_volume(connection_info)
            connection_info['data']['device_path'] = device_info['path']
            try:
                volmap.connection_info = jsonutils.dumps(connection_info)
            except TypeError:
                pass
            volmap.save(context)
            devpath = connection_info['data']['device_path']
        else:
            # this is the first time to attach the volume
            devpath = cinder.attach_volume(volmap)

        try:
            self._mount_device(volmap, devpath)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("Failed to mount device")
                try:
                    cinder.detach_volume(context, volmap)
                except Exception:
                    LOG.exception("Failed to detach volume")

    def _mount_device(self, volmap, devpath):
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        fileutils.ensure_tree(mountpoint)
        mount.do_mount(devpath, mountpoint, CONF.volume.fstype)

    @validate_volume_provider(supported_providers)
    def detach(self, context, volmap):
        self._unmount_device(volmap)
        cinder = cinder_workflow.CinderWorkflow(context)
        cinder.detach_volume(context, volmap)

    @validate_volume_provider(supported_providers)
    def delete(self, context, volmap):
        cinder = cinder_workflow.CinderWorkflow(context)
        cinder.delete_volume(volmap)

    def _unmount_device(self, volmap):
        if hasattr(volmap, 'connection_info'):
            mountpoint = mount.get_mountpoint(volmap.volume.uuid)
            mount.do_unmount(mountpoint)
            shutil.rmtree(mountpoint)

    @validate_volume_provider(supported_providers)
    def bind_mount(self, context, volmap):
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        return mountpoint, volmap.container_path

    @validate_volume_provider(supported_providers)
    def get_volume_status(self, context, volmap):
        ca = cinder_api.CinderAPI(context)
        return ca.get(volmap.cinder_volume_id).status

    @validate_volume_provider(supported_providers)
    def check_multiattach(self, context, volmap):
        ca = cinder_api.CinderAPI(context)
        return ca.get(volmap.cinder_volume_id).multiattach

    @validate_volume_provider(supported_providers)
    def is_volume_available(self, context, volmap):
        status = self.get_volume_status(context, volmap)
        if status == 'available':
            is_available = True
            is_error = False
        elif status == 'in-use':
            multiattach = self.check_multiattach(context, volmap)
            is_available = multiattach
            is_error = False
        elif status == 'error':
            is_available = False
            is_error = True
        else:
            is_available = False
            is_error = False

        return is_available, is_error

    @validate_volume_provider(supported_providers)
    def is_volume_deleted(self, context, volmap):
        try:
            volume = cinder_api.CinderAPI(context).search_volume(
                volmap.cinder_volume_id)
            is_deleted = False
            # Cinder volume error states: 'error', 'error_deleting',
            # 'error_backing-up', 'error_restoring', 'error_extending',
            # all of which start with 'error'
            is_error = True if 'error' in volume.status else False
        except exception.VolumeNotFound:
            is_deleted = True
            is_error = False

        return is_deleted, is_error
