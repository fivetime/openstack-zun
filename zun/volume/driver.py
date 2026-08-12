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
import socket

from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils
from oslo_utils import fileutils
from stevedore import driver as stevedore_driver

from zun.common import exception
from zun.common.i18n import _
from zun.common import mount
from zun.common import utils
from zun.volume import manila
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


class NFS(VolumeDriver):
    """A shared filesystem, mounted on the node and bind mounted in.

    What backs a ReadWriteMany claim: several capsules, on several nodes,
    reading and writing the same files. A Cinder volume cannot be this --
    multiattach shares the block device, not a filesystem, and two writers
    corrupt it.

    The mount happens on the node, so the node is what Manila authorises. That
    is the deliberate, uncomfortable part of this design: the grant is a /32
    for this node, made with the tenant's own token when a capsule needing the
    share lands here, and withdrawn when the last mount of that share on this
    node goes. The grant set is always exactly the nodes running the tenant's
    pods -- never a subnet, never a standing allowance.
    """

    supported_providers = ['nfs']

    # MOUNT_TIMEOUT bounds the mount syscall. An unreachable NFS server hangs
    # a plain mount for minutes, and zun-compute is one process of green
    # threads: a hung mount starves the heartbeat, and a node that misses its
    # heartbeat is refused every operation. The same failure this codebase has
    # already paid for once, from a different direction.
    MOUNT_TIMEOUT = '60'

    @validate_volume_provider(supported_providers)
    def attach(self, context, volmap):
        opts = self._options(volmap)
        export = opts.get('export')
        if not export:
            raise exception.ZunException(_(
                'an nfs volume names no export to mount'))

        # ⚠️ Both checks refuse rather than warn, and both default to
        # refusing. The property they protect -- that only this platform can
        # read a tenant's files -- is not observable from inside a capsule: a
        # share mounted on a node another tenant's workload can reach looks
        # exactly like a private one, right up until it is read. A refusal is
        # visible to whoever can fix it; the exposure is visible to nobody.
        self._refuse_unless_node_is_ours()

        # One lock per share and node: attach and detach of the same share
        # must not interleave, or two detaches can each see the other's mount
        # still present and both skip the revoke -- leaking the grant forever.
        with lockutils.lock('knaas-share-%s' % (opts.get('shareID') or export)):
            share_id = opts.get('shareID')
            if share_id:
                self._refuse_if_grants_are_wider_than_hosts(context, share_id)
                manila.grant(context, share_id,
                             self._local_ip_for(export.split(':', 1)[0]))

            path = mount.get_mountpoint(volmap.volume.uuid)
            fileutils.ensure_tree(path)
            if os.path.ismount(path):
                # Whatever is mounted there must BE this export. A stale mount
                # of something else, silently accepted, hands the capsule a
                # filesystem it never asked for.
                self._validate_mounted(path, export)
            else:
                # nosuid,nodev: the tenant controls every byte of this
                # filesystem, and it is mounted on the host. A setuid binary
                # or a device node in it must mean nothing here.
                utils.execute('timeout', self.MOUNT_TIMEOUT,
                              'mount', '-t', 'nfs',
                              '-o', 'rw,nosuid,nodev',
                              export, path, run_as_root=True)
            self._apply_fs_group(opts, path)

    @staticmethod
    def _refuse_unless_node_is_ours():
        """Refuse to mount a tenant's files on a node we do not own outright.

        The file server authorises by client address, so the unit of trust is
        the node -- not the capsule, and not the tenant. On a node that runs
        capsules and nothing else, holding the node's identity means being the
        platform. On a node shared with a kubelet or with nova it also means
        being whatever else runs there with host networking, and that is
        somebody else's workload.

        There is a version of this that does not depend on the node: a backend
        where each share carries its own credential, or a mount performed
        inside the guest so the client is the capsule's own address. Until one
        of those exists, the safe shape is the only shape, and it has to be
        declared rather than assumed.
        """
        if not CONF.volume.host_dedicated_to_capsules:
            raise exception.ZunException(_(
                'This node does not declare itself dedicated to capsules, so '
                'it will not mount a shared filesystem: the file server '
                'authorises the node, and anything else running here with the '
                'node\'s identity would be able to read these files. Set '
                '[volume] host_dedicated_to_capsules on a node that carries no '
                'other tenant workload.'))

    @staticmethod
    def _refuse_if_grants_are_wider_than_hosts(context, share_id):
        """Refuse when someone has opened the share to more than single hosts.

        Every grant this driver makes is a /32 for one node, withdrawn when the
        last mount there goes. That is the whole of the isolation a
        host-mounted share has. One subnet rule -- added by hand, by another
        tool, by an operator solving a different problem -- replaces it with
        nothing, and no capsule can tell.

        Mounting anyway would make the platform a participant in an isolation
        it knows is not there.
        """
        wide = []
        for rule in manila.access_rules(context, share_id):
            target = (rule.get('access_to') or '').strip()
            if rule.get('access_type') != 'ip':
                # A different authorisation model entirely. Not ours to judge
                # as safe.
                wide.append('%s:%s' % (rule.get('access_type'), target))
                continue
            if '/' in target and not target.endswith('/32'):
                wide.append(target)
        if wide:
            raise exception.ZunException(_(
                'Share %(share)s is reachable from more than single hosts '
                '(%(rules)s), which is wider than the access this platform '
                'grants and withdraws per node. Refusing to mount it rather '
                'than treat it as isolated.')
                % {'share': share_id, 'rules': ', '.join(sorted(wide))})

    @staticmethod
    def _validate_mounted(path, export):
        try:
            with open('/proc/mounts') as f:
                for line in f:
                    fields = line.split()
                    if len(fields) > 1 and fields[1] == path:
                        if fields[0] != export:
                            raise exception.ZunException(_(
                                '%(path)s already carries a mount of '
                                '%(other)s, not %(export)s')
                                % {'path': path, 'other': fields[0],
                                   'export': export})
                        return
        except OSError:
            pass

    @staticmethod
    def _apply_fs_group(opts, path):
        try:
            fs_group = int(opts.get('fsGroup') or 0)
        except (TypeError, ValueError):
            return
        if fs_group <= 0:
            return
        try:
            utils.execute('chown', ':%d' % fs_group, path, run_as_root=True)
            utils.execute('chmod', 'g+rwxs', path, run_as_root=True)
        except Exception as e:
            # A server exporting with root squash refuses this. The share may
            # still be writable through its export options; failing the mount
            # over ownership would help nobody.
            LOG.warning('could not apply fsGroup to %(path)s: %(err)s',
                        {'path': path, 'err': e})

    def _detach(self, context, volmap):
        opts = self._options(volmap)
        export = opts.get('export')
        with lockutils.lock('knaas-share-%s' % (opts.get('shareID') or export)):
            self._detach_locked(context, volmap, opts, export)

    def _detach_locked(self, context, volmap, opts, export):
        path = mount.get_mountpoint(volmap.volume.uuid)
        if os.path.ismount(path):
            try:
                utils.execute('umount', path, run_as_root=True)
            except Exception as e:
                LOG.warning('could not unmount %(path)s: %(err)s',
                            {'path': path, 'err': e})
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)

        share_id = opts.get('shareID')
        if share_id and export and not self._export_still_mounted(export):
            # The last mount of this share on this node is gone; so is the
            # node's reason to reach it.
            manila.revoke(context, share_id,
                          self._local_ip_for(export.split(':', 1)[0]))

    @staticmethod
    def _export_still_mounted(export):
        """Whether any other mount of this export remains on this node.

        Two capsules of the same tenant on one node mount the same share at
        two mountpoints. Revoking the node's access when the first leaves
        would cut the second off mid-write; the grant goes only when the last
        mount does.
        """
        try:
            with open('/proc/mounts') as f:
                return any(line.split()[0] == export for line in f)
        except OSError:
            # If the mount table cannot be read, keeping the grant is the
            # smaller wrong.
            return True

    @staticmethod
    def _local_ip_for(host):
        """The address this node reaches the share server from.

        That is the address the server sees and the one the access rule must
        name. Asking the routing table beats asking a config option, which
        would be wrong on any node with more than one interface.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 2049))
            return s.getsockname()[0]
        finally:
            s.close()

    @validate_volume_provider(supported_providers)
    def detach(self, context, volmap):
        self._detach(context, volmap)

    @validate_volume_provider(supported_providers)
    def delete(self, context, volmap):
        self._detach(context, volmap)

    @validate_volume_provider(supported_providers)
    def bind_mount(self, context, volmap):
        return mount.get_mountpoint(volmap.volume.uuid), volmap.container_path

    def is_volume_available(self, context, volmap):
        return True, False

    def is_volume_deleted(self, context, volmap):
        return True, False

    @staticmethod
    def _options(volmap):
        raw = volmap.volume.contents
        if not raw:
            return {}
        try:
            return jsonutils.loads(raw)
        except ValueError:
            return {}


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

    def extend(self, context, volmap, requested_gib):
        """Grow the filesystem after Cinder has grown the device.

        Two steps, and the second is ours alone. os-brick makes the kernel see
        the larger device; then the filesystem inside it has to be grown, and
        ⚠️ that step exists here and not in nova because of where the
        filesystem lives. Nova hands a raw device to a virtual machine whose
        guest kernel grows it. This mounts the filesystem on the compute node
        and bind mounts the directory into the capsule, so the node is the only
        place that can.

        Until this runs, a pod sees the old size however large the volume is --
        which looks exactly like an expansion that did not happen.
        """
        if not volmap.connection_info:
            raise exception.ZunException(_(
                'volume %s has no connection on this node') % volmap.volume.uuid)
        conn_info = jsonutils.loads(volmap.connection_info)
        cinder = cinder_workflow.CinderWorkflow(context)
        cinder.extend_volume(conn_info, requested_gib)

        devpath = conn_info['data'].get('device_path')
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        if not devpath or not os.path.ismount(mountpoint):
            # Grown where it is attached but not mounted: nothing to resize
            # here, and the next mount reads the new size anyway.
            return
        # ⚠️ resize2fs on a mounted ext4 grows it online; the same call on an
        # unmounted one also works. What it will not do is shrink, which is
        # why this is only ever reached for a request that is larger.
        utils.execute('resize2fs', devpath, run_as_root=True)

    def _mount_device(self, volmap, devpath):
        mountpoint = mount.get_mountpoint(volmap.volume.uuid)
        fileutils.ensure_tree(mountpoint)
        mount.do_mount(devpath, mountpoint, CONF.volume.fstype)
        self._apply_fs_group(volmap, mountpoint)

    @staticmethod
    def _apply_fs_group(volmap, mountpoint):
        """Give the pod's fsGroup ownership of the volume, as a kubelet would.

        A fresh filesystem is root's, mode 0755, and the workload runs as
        whatever user its image says -- almost never root. Without this the
        volume attaches, mounts, and cannot be written to, which reads as an
        application bug on a pod that looks perfectly healthy.

        Group ownership with setgid rather than handing the user the
        directory: it is what a kubelet applies, and it means files created by
        the workload stay reachable by the group when the pod is recreated
        with a different uid.
        """
        raw = volmap.volume.contents
        if not raw:
            return
        try:
            fs_group = int(jsonutils.loads(raw).get('fsGroup') or 0)
        except (ValueError, TypeError, AttributeError):
            return
        if fs_group <= 0:
            return
        utils.execute('chown', '-R', ':%d' % fs_group, mountpoint,
                      run_as_root=True)
        utils.execute('chmod', '-R', 'g+rwX', mountpoint, run_as_root=True)
        utils.execute('chmod', 'g+s', mountpoint, run_as_root=True)

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
