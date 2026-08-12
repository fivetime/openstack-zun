#    Copyright 2016 IBM Corp.
#
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

import contextlib
import itertools
import math
import os
import time

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import periodic_task
from oslo_utils import excutils
from oslo_utils import timeutils
from oslo_utils import uuidutils

from zun.common import consts
from zun.common import context
from zun.common import exception
from zun.common.i18n import _
from zun.common import utils
from zun.common.utils import translate_exception
from zun.common.utils import wrap_container_event
from zun.common.utils import wrap_exception
from zun.compute import compute_node_tracker
from zun.compute import container_actions
import zun.conf
from zun.container import driver as driver_module
from zun.image.glance import driver as glance
from zun.network import neutron
from zun import objects
from zun.scheduler.client import report

CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)


class Manager(periodic_task.PeriodicTasks):
    """Manages the running containers."""

    def __init__(self, container_driver=None):
        super(Manager, self).__init__(CONF)
        self.driver = driver_module.load_container_driver(container_driver)
        self.capsule_driver = driver_module.load_capsule_driver()
        self.host = CONF.host
        self._resource_tracker = None
        self.reportclient = report.SchedulerReportClient()

    def _get_driver(self, container):
        if (isinstance(container, objects.Capsule) or
                isinstance(container, objects.CapsuleContainer) or
                isinstance(container, objects.CapsuleInitContainer)):
            return self.capsule_driver
        elif isinstance(container, objects.Container):
            if not isinstance(self.driver, driver_module.ContainerDriver):
                raise exception.ZunException(
                    'This host serves capsules only; the Container API is '
                    'unavailable because container_driver is %(driver)s.'
                    % {'driver': CONF.container_driver})
            return self.driver
        else:
            raise exception.ZunException('Unexpected container type: %(type)s.'
                                         % {'type': type(container)})

    def restore_running_container(self, context, container, current_status):
        if (container.status == consts.RUNNING and
                current_status == consts.STOPPED):
            LOG.debug("Container %(container_uuid)s was recorded in state "
                      "(%(old_status)s) and current state is "
                      "(%(current_status)s), triggering reboot",
                      {'container_uuid': container.uuid,
                       'old_status': container.status,
                       'current_status': current_status})
            self.container_start(context, container)

    def init_containers(self, context):
        if not isinstance(self.driver, driver_module.ContainerDriver):
            # Capsule-only host: no container was ever started through the
            # Container API here, so there is nothing to reconcile.
            return
        containers = objects.Container.list_by_host(context, self.host)
        # TODO(hongbin): init capsules as well
        local_containers, _ = self.driver.list(context)
        uuid_to_status_map = {container.uuid: container.status
                              for container in local_containers}
        for container in containers:
            current_status = uuid_to_status_map[container.uuid]
            self._init_container(context, container)
            if CONF.compute.remount_container_volume:
                self._remount_volume(context, container)
            if CONF.compute.resume_container_state:
                self.restore_running_container(context,
                                               container,
                                               current_status)

    def _init_container(self, context, container):
        """Initialize this container during zun-compute init."""

        if (container.status == consts.CREATING or
            container.task_state in [consts.CONTAINER_CREATING,
                                     consts.IMAGE_PULLING,
                                     consts.NETWORK_ATTACHING,
                                     consts.NETWORK_DETACHING,
                                     consts.SG_ADDING,
                                     consts.SG_REMOVING]):
            LOG.debug("Container %s failed to create correctly, "
                      "setting to ERROR state", container.uuid)
            container.task_state = None
            container.status = consts.ERROR
            container.status_reason = _("Container failed to create correctly")
            container.save()
            return

        if (container.status == consts.DELETING or
                container.task_state == consts.CONTAINER_DELETING):
            LOG.debug("Container %s in transitional state %s at start-up "
                      "retrying delete request",
                      container.uuid, container.task_state)
            container.task_state = None
            self.container_delete(context, container, force=True)
            return

        if container.task_state == consts.CONTAINER_REBOOTING:
            LOG.debug("Container %s in transitional state %s at start-up "
                      "retrying reboot request",
                      container.uuid, container.task_state)
            container.task_state = None
            self.container_reboot(context, container,
                                  CONF.docker.default_timeout)
            return

        if container.task_state == consts.CONTAINER_STOPPING:
            LOG.debug("Container %s in transitional state %s at start-up "
                      "retrying stop request",
                      container.uuid, container.task_state)
            container.task_state = None
            self.container_stop(context, container,
                                CONF.docker.default_timeout)
            return

        if container.task_state == consts.CONTAINER_STARTING:
            LOG.debug("Container %s in transitional state %s at start-up "
                      "retrying start request",
                      container.uuid, container.task_state)
            container.task_state = None
            self.container_start(context, container)
            return

        if container.task_state == consts.CONTAINER_PAUSING:
            container.task_state = None
            self.container_pause(context, container)
            return

        if container.task_state == consts.CONTAINER_UNPAUSING:
            container.task_state = None
            self.container_unpause(context, container)
            return

        if container.task_state == consts.CONTAINER_KILLING:
            container.task_state = None
            self.container_kill(context, container)
            return

    def _remount_volume(self, context, container):
        driver = self._get_driver(container)
        volmaps = objects.VolumeMapping.list_by_container(context,
                                                          container.uuid)
        for volmap in volmaps:
            LOG.info('Re-attaching volume %(volume_id)s to %(host)s',
                     {'volume_id': volmap.cinder_volume_id,
                      'host': CONF.host})
            try:
                driver.attach_volume(context, volmap)
            except Exception as e:
                LOG.exception("Failed to re-attach volume %(volume_id)s to "
                              "container %(container_id)s: %(error)s",
                              {'volume_id': volmap.cinder_volume_id,
                               'container_id': volmap.container_uuid,
                               'error': str(e)})
                msg = _("Internal error on recovering container volume")
                self._fail_container(context, container, msg, unset_host=False)

    def _fail_container(self, context, container, error, unset_host=False):
        try:
            self._detach_volumes(context, container)
        except Exception as e:
            LOG.exception("Failed to detach volumes: %s", str(e))

        container.status = consts.ERROR
        container.status_reason = error
        if unset_host:
            container.host = None
        container.save(context)

    def _wait_for_volumes_available(
            self, context, requested_volumes, container,
            timeout=CONF.volume.timeout_wait_volume_available,
            poll_interval=1):
        driver = self._get_driver(container)
        start_time = time.time()
        try:
            volmaps = itertools.chain.from_iterable(requested_volumes.values())
            volmap = next(volmaps)
            while time.time() - start_time < timeout:
                is_available, is_error = driver.is_volume_available(
                    context, volmap)
                if is_available:
                    volmap = next(volmaps)
                if is_error:
                    break
                time.sleep(poll_interval)
        except StopIteration:
            return
        volmaps = itertools.chain.from_iterable(requested_volumes.values())
        for volmap in volmaps:
            if volmap.auto_remove:
                try:
                    driver.delete_volume(context, volmap)
                except Exception:
                    LOG.exception("Failed to delete volume")
        msg = _("Volumes did not reach available status after "
                "%d seconds") % (timeout)
        self._fail_container(context, container, msg, unset_host=True)
        raise exception.Conflict(msg)

    def _wait_for_volumes_deleted(
            self, context, volmaps, container,
            timeout=CONF.volume.timeout_wait_volume_deleted,
            poll_interval=1):
        start_time = time.time()
        try:
            volmaps = itertools.chain(volmaps)
            volmap = next(volmaps)
            while time.time() - start_time < timeout:
                if not volmap.auto_remove:
                    volmap = next(volmaps)
                driver = self._get_driver(container)
                is_deleted, is_error = driver.is_volume_deleted(
                    context, volmap)
                if is_deleted:
                    volmap = next(volmaps)
                if is_error:
                    break
                time.sleep(poll_interval)
        except StopIteration:
            return
        msg = _("Volumes cannot be successfully deleted after "
                "%d seconds") % (timeout)
        self._fail_container(context, container, msg, unset_host=True)
        raise exception.Conflict(msg)

    def _check_support_disk_quota(self, context, container):
        driver = self._get_driver(container)
        base_device_size = driver.get_host_default_base_size()
        if base_device_size:
            # NOTE(kiennt): If default_base_size is not None, it means
            #               host storage_driver is in list ['devicemapper',
            #               windowfilter', 'zfs', 'btrfs']. The following
            #               block is to prevent Zun raises Exception every time
            #               if user do not set container's disk and
            #               default_disk less than base_device_size.
            # FIXME(kiennt): This block is too complicated. We should find
            #                new efficient way to do the check.
            if not container.disk:
                container.disk = math.ceil(max(base_device_size,
                                               CONF.default_disk))
                return
            else:
                if container.disk < base_device_size:
                    msg = _('Disk size cannot be smaller than '
                            '%(base_device_size)s.') % {
                                'base_device_size': base_device_size
                    }
                    self._fail_container(context, container,
                                         msg, unset_host=True)
                    raise exception.Invalid(msg)
        # NOTE(kiennt): Only raise Exception when user passes disk size and
        #               the disk quota feature isn't supported in host.
        if not driver.node_support_disk_quota():
            if container.disk:
                msg = _('Your host does not support disk quota feature.')
                self._fail_container(context, container, msg, unset_host=True)
                raise exception.Invalid(msg)
            LOG.warning("Ignore the configured default disk size because "
                        "the driver does not support disk quota.")
        if driver.node_support_disk_quota() and not container.disk:
            container.disk = CONF.default_disk
            return

    def container_create(self, context, limits, requested_networks,
                         requested_volumes, container, run, pci_requests=None):
        @utils.synchronized(container.uuid)
        def do_container_create():
            with utils.FinishAction(context, container_actions.CREATE,
                                    container.uuid):
                self._wait_for_volumes_available(context, requested_volumes,
                                                 container)
                self._attach_volumes(context, container, requested_volumes)
                self._check_support_disk_quota(context, container)
                created_container = self._do_container_create(
                    context, container, requested_networks, requested_volumes,
                    pci_requests, limits)
                if run:
                    self._do_container_start(context, created_container)

        utils.spawn_n(do_container_create)

    @contextlib.contextmanager
    def _update_task_state(self, context, container, task_state):
        if container.task_state is not None:
            LOG.debug('Skip updating container task state to %(task_state)s '
                      'because its current task state is: '
                      '%(current_task_state)s',
                      {'task_state': task_state,
                       'current_task_state': container.task_state})
            yield
            return

        container.task_state = task_state
        container.save(context)
        try:
            yield
        finally:
            container.task_state = None
            container.save(context)

    def _do_container_create_base(self, context, container, requested_networks,
                                  requested_volumes,
                                  limits=None):
        with self._update_task_state(context, container,
                                     consts.CONTAINER_CREATING):
            image_driver_name = container.image_driver
            repo, tag = utils.parse_image_name(container.image,
                                               image_driver_name,
                                               registry=container.registry)
            image_pull_policy = utils.get_image_pull_policy(
                container.image_pull_policy, tag)
            # By the object's own driver: on a host serving both, a capsule
            # and a container can be built by different drivers with different
            # answers to this.
            builder = self._get_driver(container)
            if not builder.pulls_own_images:
                try:
                    # TODO(hongbin): move image pulling logic to docker driver
                    image, image_loaded = builder.pull_image(
                        context, repo, tag, image_pull_policy,
                        image_driver_name, registry=container.registry)
                    image['repo'], image['tag'] = repo, tag
                    if not image_loaded:
                        builder.load_image(image['path'])
                except exception.ImageNotFound as e:
                    with excutils.save_and_reraise_exception():
                        LOG.error(str(e))
                        self._fail_container(context, container, str(e))
                except exception.DockerError as e:
                    with excutils.save_and_reraise_exception():
                        LOG.error("Error occurred while calling Docker image "
                                  "API: %s", str(e))
                        self._fail_container(context, container, str(e))
                except Exception as e:
                    with excutils.save_and_reraise_exception():
                        LOG.exception("Unexpected exception: %s",
                                      str(e))
                        self._fail_container(context, container, str(e))
            else:
                # This driver pulls images itself while creating the container
                # (the CRI driver does so per container), so there is nothing
                # to pre-pull here and no local image path to hand over.
                image = {'driver': image_driver_name, 'path': None,
                         'repo': repo, 'tag': tag}

            container.image_driver = image.get('driver')
            container.save(context)
            try:
                if image['driver'] == 'glance' and image.get('path'):
                    self.driver.read_tar_image(image)
                if image['tag'] != tag:
                    LOG.warning("The input tag is different from the tag in "
                                "tar")
                if isinstance(container, objects.Capsule):
                    container = self.capsule_driver.create_capsule(
                        context, container, image, requested_networks,
                        requested_volumes)
                elif isinstance(container, objects.Container):
                    container = self.driver.create(context, container, image,
                                                   requested_networks,
                                                   requested_volumes)
                return container
            except exception.DockerError as e:
                with excutils.save_and_reraise_exception():
                    LOG.error("Error occurred while calling Docker create "
                              "API: %s", str(e))
                    self._fail_container(context, container, str(e),
                                         unset_host=True)
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    LOG.exception("Unexpected exception: %s",
                                  str(e))
                    self._fail_container(context, container, str(e),
                                         unset_host=True)

    @wrap_container_event(prefix='compute')
    def _do_container_create(self, context, container, requested_networks,
                             requested_volumes, pci_requests=None,
                             limits=None):
        LOG.debug('Creating container: %s', container.uuid)

        try:
            rt = self._get_resource_tracker()
            with rt.container_claim(context, container, pci_requests, limits):
                created_container = self._do_container_create_base(
                    context, container, requested_networks, requested_volumes,
                    limits)
                return created_container
        except exception.ResourcesUnavailable as e:
            with excutils.save_and_reraise_exception():
                LOG.exception("Container resource claim failed: %s",
                              str(e))
                self._fail_container(context, container, str(e),
                                     unset_host=True)
                self.reportclient.delete_allocation_for_container(
                    context, container.uuid)
        except Exception as e:
            # ⚠️ Every OTHER failure has to give the claim back too, and this
            # branch did not exist. The claim is written to placement before
            # the container is built; the context manager's abort() only rolls
            # back this service's in-memory usage, so an image that would not
            # pull, a runtime that refused, a port that would not bind -- any
            # of them left placement believing the resources were still held.
            # Nothing ever collects those: they are keyed by a container uuid
            # that no longer exists, so they accumulate for the life of the
            # deployment until the node looks full and schedules nothing.
            #
            # Measured on the lab after a day of a controller recreating pods
            # in a loop: 241 allocations against two nodes whose live capsule
            # count was 22 and 0. Deleting is safe on any path -- placement
            # returns 404 for an allocation that is already gone, and the
            # reportclient treats that as success.
            with excutils.save_and_reraise_exception():
                LOG.exception("Container create failed, releasing its "
                              "placement allocation: %s", str(e))
                try:
                    self.reportclient.delete_allocation_for_container(
                        context, container.uuid)
                except Exception:
                    # Never let cleanup mask the real failure above.
                    LOG.exception("Could not release the placement allocation "
                                  "for %s; it will need reclaiming",
                                  container.uuid)

    def _attach_volumes_for_capsule(self, context, capsule, requested_volumes):
        for c in (capsule.init_containers or []):
            self._attach_volumes(context, c, requested_volumes)
        for c in (capsule.containers or []):
            self._attach_volumes(context, c, requested_volumes)

    def _attach_volumes(self, context, container, requested_volumes):
        if isinstance(container, objects.Capsule):
            self._attach_volumes_for_capsule(context, container,
                                             requested_volumes)
            return

        try:
            volmaps = requested_volumes.get(container.uuid, [])
            for volmap in volmaps:
                volmap.container_uuid = container.uuid
                volmap.host = self.host
                volmap.create(context)
                if (volmap.connection_info and
                        (isinstance(container, objects.CapsuleContainer) or
                         isinstance(container, objects.CapsuleInitContainer))):
                    # NOTE(hongbin): In this case, the volume is already
                    # attached to this host so we don't need to do it again.
                    # This will happen only if there are multiple containers
                    # inside a capsule sharing the same volume.
                    continue
                self._attach_volume(context, container, volmap)
                self._refresh_attached_volumes(requested_volumes, volmap)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                self._fail_container(context, container, str(e),
                                     unset_host=True)

    def _attach_volume(self, context, container, volmap):
        driver = self._get_driver(container)
        context = context.elevated()
        LOG.info('Attaching volume %(volume_id)s to %(host)s',
                 {'volume_id': volmap.cinder_volume_id,
                  'host': CONF.host})
        try:
            driver.attach_volume(context, volmap)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error("Failed to attach volume %(volume_id)s to "
                          "container %(container_id)s",
                          {'volume_id': volmap.cinder_volume_id,
                           'container_id': volmap.container_uuid})
                if volmap.auto_remove:
                    try:
                        driver.delete_volume(context, volmap)
                    except Exception:
                        LOG.exception("Failed to delete volume %s.",
                                      volmap.cinder_volume_id)
                volmap.destroy()

    def _refresh_attached_volumes(self, requested_volumes, attached_volmap):
        volmaps = itertools.chain.from_iterable(requested_volumes.values())
        for volmap in volmaps:
            if volmap.volume_id != attached_volmap.volume_id:
                continue
            if (volmap.obj_attr_is_set('uuid') and
                    volmap.uuid == attached_volmap.uuid):
                continue
            volmap.volume.refresh()

    def _detach_volumes_for_capsule(self, context, capsule, reraise):
        for c in (capsule.init_containers or []):
            self._detach_volumes(context, c, reraise)
        for c in (capsule.containers or []):
            self._detach_volumes(context, c, reraise)

    def _detach_volumes(self, context, container, reraise=True):
        if isinstance(container, objects.Capsule):
            self._detach_volumes_for_capsule(context, container, reraise)
            return

        volmaps = objects.VolumeMapping.list_by_container(context,
                                                          container.uuid)
        auto_remove_volmaps = []
        for volmap in volmaps:
            db_volmaps = objects.VolumeMapping.list_by_cinder_volume(
                context, volmap.cinder_volume_id)
            self._detach_volume(context, container, volmap, reraise=reraise)
            if volmap.auto_remove and len(db_volmaps) == 1:
                self._get_driver(container).delete_volume(context, volmap)
                auto_remove_volmaps.append(volmap)
        self._wait_for_volumes_deleted(context, auto_remove_volmaps, container)

    def _detach_volume(self, context, container, volmap, reraise=True):
        if objects.VolumeMapping.count(
                context, volume_id=volmap.volume_id) == 1:
            context = context.elevated()
            try:
                self._get_driver(container).detach_volume(context, volmap)
            except Exception:
                with excutils.save_and_reraise_exception(reraise=reraise):
                    LOG.error("Failed to detach volume %(volume_id)s from "
                              "container %(container_id)s",
                              {'volume_id': volmap.cinder_volume_id,
                               'container_id': volmap.container_uuid})
        volmap.destroy()

    @wrap_container_event(prefix='compute')
    def _do_container_start(self, context, container):
        LOG.debug('Starting container: %s', container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_STARTING):
            try:
                # NOTE(hongbin): capsule shouldn't reach here
                container = self.driver.start(context, container)
                container.started_at = timeutils.utcnow()
                container.save(context)
                return container
            except exception.DockerError as e:
                with excutils.save_and_reraise_exception():
                    LOG.error("Error occurred while calling Docker start "
                              "API: %s", str(e))
                    self._fail_container(context, container, str(e))
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    LOG.exception("Unexpected exception: %s",
                                  str(e))
                    self._fail_container(context, container, str(e))

    @translate_exception
    def container_delete(self, context, container, force=False):
        @utils.synchronized(container.uuid)
        def do_container_delete():
            self._do_container_delete(context, container, force)

        utils.spawn_n(do_container_delete)

    def _do_container_delete(self, context, container, force):
        LOG.debug('Deleting container: %s', container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_DELETING):
            reraise = not force
            try:
                if isinstance(container, objects.Capsule):
                    self.capsule_driver.delete_capsule(context, container,
                                                       force)
                elif isinstance(container, objects.Container):
                    self.driver.delete(context, container, force)
            except exception.DockerError as e:
                with excutils.save_and_reraise_exception(reraise=reraise):
                    LOG.error("Error occurred while calling Docker  "
                              "delete API: %s", str(e))
                    self._fail_container(context, container, str(e))
            except Exception as e:
                with excutils.save_and_reraise_exception(reraise=reraise):
                    LOG.exception("Unexpected exception: %s", str(e))
                    self._fail_container(context, container, str(e))

            self._detach_volumes(context, container, reraise=reraise)

        # Remove the claimed resource
        rt = self._get_resource_tracker()
        rt.remove_usage_from_container(context, container, True)
        self.reportclient.delete_allocation_for_container(context,
                                                          container.uuid)
        # only destroy the container in the db if the
        # delete_allocation_for_instance doesn't raise and therefore
        # allocation is successfully deleted in placement
        container.destroy(context)

    def add_security_group(self, context, container, security_group):
        @utils.synchronized(container.uuid)
        def do_add_security_group():
            self._add_security_group(context, container, security_group)

        utils.spawn_n(do_add_security_group)

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.ADD_SECURITY_GROUP)
    def _add_security_group(self, context, container, security_group):
        LOG.debug('Adding security_group to container: %s', container.uuid)
        with self._update_task_state(context, container, consts.SG_ADDING):
            # NOTE(hongbin): capsule shouldn't reach here
            self.driver.add_security_group(context, container, security_group)
            container.security_groups += [security_group]
            container.save(context)

    def remove_security_group(self, context, container, security_group):
        @utils.synchronized(container.uuid)
        def do_remove_security_group():
            self._remove_security_group(context, container, security_group)

        utils.spawn_n(do_remove_security_group)

    @wrap_exception()
    @wrap_container_event(
        prefix='compute',
        finish_action=container_actions.REMOVE_SECURITY_GROUP)
    def _remove_security_group(self, context, container, security_group):
        LOG.debug('Removing security_group from container: %s', container.uuid)
        with self._update_task_state(context, container, consts.SG_REMOVING):
            # NOTE(hongbin): capsule shouldn't reach here
            self.driver.remove_security_group(context, container,
                                              security_group)
            container.security_groups = list(set(container.security_groups)
                                             - set([security_group]))
            container.save(context)

    @translate_exception
    def container_show(self, context, container):
        LOG.debug('Showing container: %s', container.uuid)
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.show(context, container)
            if container.obj_what_changed():
                container.save(context)
            return container
        except exception.DockerError as e:
            LOG.error("Error occurred while calling Docker show API: %s",
                      str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.REBOOT)
    def _do_container_reboot(self, context, container, timeout):
        LOG.debug('Rebooting container: %s', container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_REBOOTING):
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.reboot(context, container, timeout)
            return container

    def container_reboot(self, context, container, timeout):
        @utils.synchronized(container.uuid)
        def do_container_reboot():
            self._do_container_reboot(context, container, timeout)

        utils.spawn_n(do_container_reboot)

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.STOP)
    def _do_container_stop(self, context, container, timeout):
        LOG.debug('Stopping container: %s', container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_STOPPING):
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.stop(context, container, timeout)
            return container

    def container_stop(self, context, container, timeout):
        @utils.synchronized(container.uuid)
        def do_container_stop():
            self._do_container_stop(context, container, timeout)

        utils.spawn_n(do_container_stop)

    def _update_container_state(self, context, container, container_status):
        if container.status != container_status:
            container.status = container_status
            container.save(context)

    def container_rebuild(self, context, container, run):
        @utils.synchronized(container.uuid)
        def do_container_rebuild():
            self._do_container_rebuild(context, container, run)

        utils.spawn_n(do_container_rebuild)

    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.REBUILD)
    def _do_container_rebuild(self, context, container, run):
        LOG.info("start to rebuild container: %s", container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_REBUILDING):
            vol_info = {container.uuid: self._get_vol_info(context, container)}
            try:
                network_info = self._get_network_info(context, container)
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    self._fail_container(context, container, str(e))
            # NOTE(hongbin): capsule shouldn't reach here
            if self.driver.check_container_exist(container):
                for addr in container.addresses.values():
                    for port in addr:
                        port['preserve_on_delete'] = True

                try:
                    # NOTE(hongbin): capsule shouldn't reach here
                    self.driver.delete(context, container, True)
                except Exception as e:
                    with excutils.save_and_reraise_exception():
                        LOG.error("Rebuild container: %s failed, "
                                  "reason of failure is: %s",
                                  container.uuid,
                                  str(e))
                        self._fail_container(context, container,
                                             str(e))

            try:
                created_container = self._do_container_create_base(
                    context, container, network_info, vol_info)
                created_container.status = consts.CREATED
                created_container.status_reason = None
                created_container.save(context)
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    LOG.error("Rebuild container:%s failed, "
                              "reason of failure is: %s", container.uuid, e)
                    self._fail_container(context, container, str(e))

            LOG.info("rebuild container: %s success", created_container.uuid)
            if run:
                self._do_container_start(context, created_container)

    def _get_vol_info(self, context, container):
        return objects.VolumeMapping.list_by_container(context,
                                                       container.uuid)

    def _get_network_info(self, context, container):
        neutron_api = neutron.NeutronAPI(context)
        network_info = []
        for network_id in container.addresses:
            try:
                addr_info = container.addresses[network_id][0]
                port_id = addr_info.get('port')
                neutron_api.get_neutron_port(port_id)
                network = neutron_api.get_neutron_network(network_id)
            except exception.PortNotFound:
                LOG.exception("The port: %s used by the source container "
                              "does not exist, can not rebuild", port_id)
                raise
            except exception.NetworkNotFound:
                LOG.exception("The network: %s used by the source container "
                              "does not exist, can not rebuild", network_id)
                raise
            except Exception as e:
                LOG.exception("Unexpected exception: %s", e)
                raise
            preserve_info = addr_info.get('preserve_on_delete')
            network_info.append({'network': network_id,
                                 'port': port_id,
                                 'router:external':
                                     network.get('router:external'),
                                 'shared': network.get('shared'),
                                 'fixed_ip': '',
                                 'preserve_on_delete': preserve_info})
        return network_info

    def container_start(self, context, container):
        @utils.synchronized(container.uuid)
        def do_container_start():
            with utils.FinishAction(context, container_actions.START,
                                    container.uuid):
                self._do_container_start(context, container)

        utils.spawn_n(do_container_start)

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.PAUSE)
    def _do_container_pause(self, context, container):
        LOG.debug('Pausing container: %s', container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_PAUSING):
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.pause(context, container)
            return container

    def container_pause(self, context, container):
        @utils.synchronized(container.uuid)
        def do_container_pause():
            self._do_container_pause(context, container)

        utils.spawn_n(do_container_pause)

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.UNPAUSE)
    def _do_container_unpause(self, context, container):
        LOG.debug('Unpausing container: %s', container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_UNPAUSING):
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.unpause(context, container)
            return container

    def container_unpause(self, context, container):
        @utils.synchronized(container.uuid)
        def do_container_unpause():
            self._do_container_unpause(context, container)

        utils.spawn_n(do_container_unpause)

    @translate_exception
    def container_logs(self, context, container, stdout, stderr,
                       timestamps, tail, since):
        LOG.debug('Showing container logs: %s', container.uuid)
        try:
            # A capsule's container is served by the capsule driver, which on a
            # host running docker for containers is a different one.
            return self._get_driver(container).show_logs(context, container,
                                         stdout=stdout, stderr=stderr,
                                         timestamps=timestamps, tail=tail,
                                         since=since)
        except exception.DockerError as e:
            LOG.error("Error occurred while calling Docker logs API: %s",
                      str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @translate_exception
    def container_exec(self, context, container, command, run, interactive):
        LOG.debug('Executing command in container: %s', container.uuid)
        try:
            # By the container's own type, not by which driver this service
            # was configured with: a capsule's container is served by the
            # capsule driver even on a host whose container driver is
            # something else, and reaching the wrong one execs into nothing.
            driver = self._get_driver(container)
            exec_id = driver.execute_create(context, container, command,
                                            interactive)
            if run:
                output, exit_code = driver.execute_run(exec_id, command)
                return {"output": output,
                        "exit_code": exit_code,
                        "exec_id": None,
                        "token": None}
            else:
                token = uuidutils.generate_uuid()
                # Where the session actually lives. A runtime that serves its
                # own streams answers with a URL of its own -- on this node's
                # loopback, which is why something on this node has to proxy
                # it -- and only a daemon-backed driver falls back to a
                # configured endpoint.
                url = getattr(driver, 'exec_stream_url', lambda h: None)(exec_id)
                if not url:
                    url = CONF.docker.docker_remote_api_url
                exec_instace = objects.ExecInstance(
                    context, container_id=container.id, exec_id=exec_id,
                    url=url, token=token)
                exec_instace.create(context)
                return {'output': None,
                        'exit_code': None,
                        'exec_id': exec_id,
                        'token': token,
                        # Which proxy to reach this session through. It is
                        # this node's, and only this node's: the runtime
                        # serves the stream on loopback here, so the proxy
                        # that can reach it runs here too. The API cannot
                        # know that from its own configuration -- it would
                        # name its own host, which serves nothing.
                        'proxy_base': CONF.websocket_proxy.base_url}
        except exception.DockerError as e:
            LOG.error("Error occurred while calling Docker exec API: %s",
                      str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @translate_exception
    def container_exec_resize(self, context, exec_id, height, width):
        LOG.debug('Resizing the tty session used by the exec: %s', exec_id)
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            return self.driver.execute_resize(exec_id, height, width)
        except exception.DockerError as e:
            LOG.error("Error occurred while calling Docker exec API: %s",
                      str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.KILL)
    def _do_container_kill(self, context, container, signal):
        LOG.debug('Killing a container: %s', container.uuid)
        with self._update_task_state(context, container,
                                     consts.CONTAINER_KILLING):
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.kill(context, container, signal)
            return container

    def container_kill(self, context, container, signal):
        @utils.synchronized(container.uuid)
        def do_container_kill():
            self._do_container_kill(context, container, signal)

        utils.spawn_n(do_container_kill)

    @translate_exception
    def container_update(self, context, container, patch):
        LOG.debug('Updating a container: %s', container.uuid)
        old_container = container.obj_clone()
        # Update only the fields that have changed
        for field, patch_val in patch.items():
            if getattr(container, field) != patch_val:
                setattr(container, field, patch_val)

        try:
            rt = self._get_resource_tracker()
            # TODO(hongbin): limits should be populated by scheduler
            # FIXME(hongbin): rt.compute_node could be None
            cpu_limit = (rt.compute_node.cpus *
                         self.driver.get_cpu_allocation_ratio())
            memory_limit = (rt.compute_node.mem_total *
                            self.driver.get_ram_allocation_ratio())
            limits = {'cpu': cpu_limit,
                      'memory': memory_limit}
            if container.cpu_policy == 'dedicated':
                limits['cpuset'] = self._get_cpuset_limits(rt.compute_node,
                                                           container)
            with rt.container_update_claim(context, container, old_container,
                                           limits):
                # NOTE(hongbin): capsule shouldn't reach here
                self.driver.update(context, container)
                container.save(context)
            return container
        except exception.ResourcesUnavailable as e:
            with excutils.save_and_reraise_exception():
                LOG.exception("Update container resource claim failed: %s",
                              str(e))
        except exception.DockerError as e:
            LOG.error("Error occurred while calling docker API: %s",
                      str(e))
            raise

    @translate_exception
    def container_attach(self, context, container):
        LOG.debug('Get websocket url from the container: %s', container.uuid)
        try:
            driver = self._get_driver(container)
            url = driver.get_websocket_url(context, container)
            token = uuidutils.generate_uuid()
            container.websocket_url = url
            container.websocket_token = token
            container.save(context)
            # Which proxy reaches this session, said by the node that knows.
            # A runtime serving its own streams puts them on this node's
            # loopback; the API host's own setting names a proxy that serves
            # nothing. Same correction the interactive exec path needed.
            return {'token': token,
                    'proxy_base': CONF.websocket_proxy.base_url}
        except Exception as e:
            LOG.error("Error occurred while calling "
                      "get websocket url function: %s",
                      str(e))
            raise

    @translate_exception
    def container_resize(self, context, container, height, width):
        LOG.debug('Resize tty to the container: %s', container.uuid)
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.resize(context, container, height, width)
            return container
        except exception.DockerError as e:
            LOG.error("Error occurred while calling docker "
                      "resize API: %s",
                      str(e))
            raise

    @translate_exception
    def container_top(self, context, container, ps_args):
        LOG.debug('Displaying the running processes inside the container: %s',
                  container.uuid)
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            return self.driver.top(context, container, ps_args)
        except exception.DockerError as e:
            LOG.error("Error occurred while calling Docker top API: %s",
                      str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @translate_exception
    def container_get_archive(self, context, container, path, encode_data):
        LOG.debug('Copying resource from the container: %s', container.uuid)
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            filedata, stat = self.driver.get_archive(context, container, path)
            if encode_data:
                filedata = utils.encode_file_data(filedata)
            return filedata, stat
        except exception.DockerError as e:
            LOG.error(
                "Error occurred while calling Docker get_archive API: %s",
                str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @translate_exception
    def container_put_archive(self, context, container, path, data,
                              decode_data):
        LOG.debug('Copying resource to the container: %s', container.uuid)
        if decode_data:
            data = utils.decode_file_data(data)
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            return self.driver.put_archive(context, container, path, data)
        except exception.DockerError as e:
            LOG.error(
                "Error occurred while calling Docker put_archive API: %s",
                str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @translate_exception
    def capsule_extend_volume(self, context, capsule, volume_id, requested_gib):
        """Grow a capsule's block volume after Cinder has grown it.

        ⚠️ The caller has already extended the volume; this is the half that
        can only be done where the volume is attached. os-brick makes the
        kernel see the new size and the filesystem is grown on top of it --
        and until that happens the pod sees the old size however large the
        volume is, which is indistinguishable from an expansion that did not
        work.
        """
        LOG.debug('Extending volume %(vol)s of capsule %(cap)s to %(gib)sGiB',
                  {'vol': volume_id, 'cap': capsule.uuid, 'gib': requested_gib})
        # ⚠️ A capsule's volumes belong to the containers inside it, not to the
        # capsule: that is how they are attached (_attach_volumes_for_capsule),
        # so it is how they have to be found. Looking them up under the
        # capsule's own uuid finds nothing and reads as "not attached here".
        driver = self._get_driver(capsule)
        found = False
        for c in list(capsule.init_containers or []) + list(capsule.containers or []):
            for volmap in objects.VolumeMapping.list_by_container(context, c.uuid):
                if volmap.cinder_volume_id != volume_id:
                    continue
                driver.extend_volume(context.elevated(), volmap,
                                     int(requested_gib))
                found = True
        if not found:
            raise exception.ZunException(_(
                'capsule %(cap)s has no volume %(vol)s attached here')
                % {'cap': capsule.uuid, 'vol': volume_id})

    def capsule_update_file(self, context, capsule, container_path, contents):
        """Replace the contents of one of a capsule's file volumes, in place.

        A capsule's file volumes are written once, when it is built, and a
        capsule cannot be changed afterwards -- so a file that has to change
        while the workload runs had no way to. The case that forces this is a
        service account token: it expires, and the alternatives are all worse.
        Recreating the capsule restarts the workload and takes its address with
        it. Running a command inside the container to rewrite the file needs a
        shell, and the images most worth running do not have one -- a
        distroless image has no `sh`, so that path fails on exactly the
        well-built images it would matter most for.

        The file is written where it already is, not replaced. It is bind
        mounted into the container, so truncating and rewriting is seen there
        immediately; creating a new file and renaming it over the old one
        breaks the mount, and the container would go on reading the file that
        is no longer there.

        Only a local file volume can be rewritten. A Cinder volume is a block
        device and this is not what it means to write to one.
        """
        volmaps = objects.VolumeMapping.list_by_container(context, capsule.uuid)
        for volmap in volmaps:
            if volmap.container_path != container_path:
                continue
            if volmap.volume.volume_provider != 'local':
                raise exception.Invalid(
                    _('%(path)s is not a file volume') % {
                        'path': container_path})
            self.driver.update_file_volume(context, volmap, contents)
            volmap.contents = contents
            volmap.save(context)
            return

        raise exception.VolumeMappingNotFound(id=container_path)

    @translate_exception
    def capsule_stats(self, context, capsule):
        """Resource usage of every container in a capsule.

        Separate from container_stats because a capsule is not a container
        here: it is a sandbox holding several, and the answer is per-container.
        A capsule driver that cannot report usage says so, rather than
        answering an empty set that reads as "running and using nothing".
        """
        LOG.debug('Displaying stats of the capsule: %s', capsule.uuid)
        if not hasattr(self.capsule_driver, 'capsule_stats'):
            raise exception.OperationNotSupported(
                message=_('Resource usage is not reported by capsule '
                          'driver %s') % type(self.capsule_driver).__name__)
        return self.capsule_driver.capsule_stats(context, capsule)

    @translate_exception
    def container_stats(self, context, container):
        LOG.debug('Displaying stats of the container: %s', container.uuid)
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            return self.driver.stats(context, container)
        except exception.DockerError as e:
            LOG.error("Error occurred while calling Docker stats API: %s",
                      str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s", str(e))
            raise

    @translate_exception
    def container_commit(self, context, container, repository, tag=None):
        LOG.debug('Committing the container: %s', container.uuid)
        snapshot_image = None
        try:
            # NOTE(miaohb): Glance is the only driver that support image
            # uploading in the current version, so we have hard-coded here.
            # https://bugs.launchpad.net/zun/+bug/1697342
            # NOTE(hongbin): capsule shouldn't reach here
            snapshot_image = self.driver.create_image(context, repository,
                                                      glance.GlanceDriver())
        except exception.DockerError as e:
            LOG.error("Error occurred while calling glance "
                      "create_image API: %s",
                      str(e))

        @utils.synchronized(container.uuid)
        def do_container_commit():
            self._do_container_commit(context, snapshot_image, container,
                                      repository, tag)

        utils.spawn_n(do_container_commit)
        return {"uuid": snapshot_image.id}

    def _do_container_image_upload(self, context, snapshot_image,
                                   container_image_id, data, tag):
        try:
            # NOTE(hongbin): capsule shouldn't reach here
            self.driver.upload_image_data(context, snapshot_image,
                                          tag, data, glance.GlanceDriver())
        except Exception as e:
            LOG.exception("Unexpected exception while uploading image: %s",
                          str(e))
            # NOTE(hongbin): capsule shouldn't reach here
            self.driver.delete_committed_image(context, snapshot_image.id,
                                               glance.GlanceDriver())
            self.driver.delete_image(context, container_image_id,
                                     'docker')
            raise

    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.COMMIT)
    def _do_container_commit(self, context, snapshot_image, container,
                             repository, tag=None):
        container_image_id = None
        LOG.debug('Creating image...')
        if tag is None:
            tag = 'latest'

        # ensure the container is paused before doing commit
        unpause = False
        if container.status == consts.RUNNING:
            # NOTE(hongbin): capsule shouldn't reach here
            container = self.driver.pause(context, container)
            container.save(context)
            unpause = True

        try:
            # NOTE(hongbin): capsule shouldn't reach here
            container_image_id = self.driver.commit(context, container,
                                                    repository, tag)
            container_image = self.driver.get_image(repository + ':' + tag)
        except exception.DockerError as e:
            LOG.error("Error occurred while calling docker commit API: %s",
                      str(e))
            # NOTE(hongbin): capsule shouldn't reach here
            self.driver.delete_committed_image(context, snapshot_image.id,
                                               glance.GlanceDriver())
            raise
        finally:
            if unpause:
                try:
                    # NOTE(hongbin): capsule shouldn't reach here
                    container = self.driver.unpause(context, container)
                    container.save(context)
                except Exception as e:
                    LOG.exception("Unexpected exception: %s", str(e))

        LOG.debug('Upload image %s to glance', container_image_id)
        self._do_container_image_upload(context, snapshot_image,
                                        container_image_id,
                                        container_image, tag)

    def image_delete(self, context, image):
        utils.spawn_n(self._do_image_delete, context, image)

    def _do_image_delete(self, context, image):
        LOG.debug('Deleting image...')
        # TODO(hongbin): Let caller pass down image_driver instead of using
        # CONF.default_image_driver
        if image.image_id:
            self.driver.delete_image(context, image.image_id)
        image.destroy(context, image.uuid)

    def image_pull(self, context, image):
        utils.spawn_n(self._do_image_pull, context, image)

    def _do_image_pull(self, context, image):
        LOG.debug('Creating image...')
        image_driver_name = CONF.default_image_driver
        repo_tag = image.repo
        if image.tag:
            repo_tag += ":" + image.tag
        if uuidutils.is_uuid_like(image.repo):
            image.tag = ''
            image_driver_name = 'glance'
        try:
            pulled_image, image_loaded = self.driver.pull_image(
                context, image.repo, image.tag, driver_name=image_driver_name)
            if not image_loaded:
                self.driver.load_image(pulled_image['path'])

            if pulled_image['driver'] == 'glance':
                self.driver.read_tar_image(pulled_image)
                if pulled_image['tag'] not in pulled_image['tags']:
                    LOG.warning("The glance image tag %(glance_tag)s is "
                                "different from %(tar_tag)s the tag in tar",
                                {'glance_tag': pulled_image['tags'],
                                 'tar_tag': pulled_image['tag']})
                repo_tag = ':'.join([pulled_image['repo'],
                                     pulled_image['tag']]) \
                    if pulled_image['tag'] else pulled_image['repo']
            image_dict = self.driver.inspect_image(repo_tag)

            image_parts = image_dict['RepoTags'][0].split(":", 1)
            image.repo = image_parts[0]
            image.tag = image_parts[1]
            image.image_id = image_dict['Id']
            image.size = image_dict['Size']
            image.save()
        except exception.ImageNotFound as e:
            LOG.error(str(e))
            return
        except exception.DockerError as e:
            LOG.error("Error occurred while calling Docker image API: %s",
                      str(e))
            raise
        except Exception as e:
            LOG.exception("Unexpected exception: %s",
                          str(e))
            raise

    @translate_exception
    def image_search(self, context, image, image_driver_name, exact_match,
                     registry):
        LOG.debug('Searching image...', image=image)
        repo, tag = utils.parse_image_name(image, image_driver_name,
                                           registry=registry)
        try:
            return self.driver.search_image(context, repo, tag,
                                            image_driver_name, exact_match)
        except Exception as e:
            LOG.exception("Unexpected exception while searching image: %s",
                          str(e))
            raise

    @periodic_task.periodic_task(run_immediately=True)
    def inventory_host(self, context):
        rt = self._get_resource_tracker()
        rt.update_available_resources(context)

    def _get_cpuset_limits(self, compute_node, container):
        for numa_node in compute_node.numa_topology.nodes:
            if len(numa_node.cpuset) - len(
                    numa_node.pinned_cpus) >= container.cpu and \
                    numa_node.mem_available >= container.memory:
                return {
                    'node': numa_node.id,
                    'cpuset_cpu': numa_node.cpuset,
                    'cpuset_cpu_pinned': numa_node.pinned_cpus,
                    'cpuset_mem': numa_node.mem_available
                }
        msg = _("There may be not enough numa resources.")
        raise exception.NoValidHost(reason=msg)

    def _get_resource_tracker(self):
        if not self._resource_tracker:
            rt = compute_node_tracker.ComputeNodeTracker(
                self.host, self.driver, self.capsule_driver, self.reportclient)
            self._resource_tracker = rt
        return self._resource_tracker

    @periodic_task.periodic_task(run_immediately=True)
    def delete_unused_containers(self, context):
        """Delete container with status DELETED"""
        # NOTE(kiennt): Need to filter with both status (DELETED) and
        #               task_state (None). If task_state in
        #               [CONTAINER_DELETING] it may
        #               raise some errors when try to delete container.
        filters = {
            'auto_remove': True,
            'status': consts.DELETED,
            'task_state': None,
            # This node's only: every compute node runs this task, and an
            # unfiltered list has each of them trying to delete containers that
            # live somewhere else.
            'host': self.host,
        }
        containers = objects.Container.list(context, filters=filters)

        if containers:
            for container in containers:
                try:
                    msg = ('%(behavior)s deleting container '
                           '%(container_name)s with status DELETED')
                    LOG.info(msg, {'behavior': 'Start',
                                   'container_name': container.name})
                    self.container_delete(context, container, True)
                    LOG.info(msg, {'behavior': 'Complete',
                                   'container_name': container.name})
                except exception.DockerError:
                    return
                except Exception:
                    return

    @periodic_task.periodic_task(spacing=CONF.probe_check_interval,
                                 run_immediately=True)
    @context.set_context
    def check_container_probes(self, ctx):
        """Run probes that are due.

        On its own clock rather than the state sync's: a probe has the period
        its author asked for, and sharing the sync interval made every probe
        run at that interval however short a period was declared.
        """
        if not hasattr(self.capsule_driver, 'check_probes'):
            return
        # Only this node's capsules. Every compute node runs this task, and an
        # unfiltered list has each of them probing every capsule in the
        # deployment — the ones they do not host have no container to exec
        # into, so those probes fail and overwrite the result of the node that
        # actually ran them.
        self.capsule_driver.check_probes(
            ctx, objects.Capsule.list_by_host(ctx, self.host))

    @periodic_task.periodic_task(
        spacing=CONF.compute.reclaim_orphan_ports_interval)
    @context.set_context
    def reclaim_orphan_ports(self, ctx):
        """Delete Neutron ports whose container no longer exists.

        A port is created for a container and deleted with it. When the delete
        path fails before it gets that far -- and for five days it did, waiting
        on a shim that was gone -- the port outlives everything that knows
        about it: the container row is hard-deleted, so nothing is left that
        could ever connect the two again. Measured here: 136 such ports on one
        tenant, against 8 running pods, on a /24 where 152 of 253 addresses
        were spoken for. The wall this ends at is "no address available", and
        nothing at that point points back at a capsule deleted last week.

        ⚠️ Only ports whose real owner is something outside Zun -- today a
        Kubernetes pod, marked at creation by network/neutron.py. A capsule or
        container a tenant made through the Zun API is theirs: its port may be
        one they mean to keep, and this has no way to know. Zun's own leak is
        not a licence to tidy somebody else's project.
        """
        if not CONF.compute.reclaim_orphan_ports:
            return

        # ⚠️ The context the decorator handed in, not a fresh admin one.
        # set_context builds it with all_projects=True; get_admin_context()
        # defaults to False, and a container list missing every other tenant's
        # rows would make their ports look orphaned and delete them.
        neutron_api = neutron.NeutronAPI(ctx)
        try:
            ports = neutron_api.list_ports(
                device_owner=consts.DEVICE_OWNER_ZUN,
                **{consts.BINDING_HOST_ID: self.host})['ports']
        except Exception as e:
            LOG.warning('Orphan port sweep skipped: %s', str(e))
            return

        # Every container this deployment knows of, not just this host's: a
        # port bound here can belong to a container the database has moved,
        # and deleting it because this node has not heard of it would take the
        # network from a container that is running somewhere else.
        known = {c.uuid for c in objects.Container.list(ctx)}

        now = timeutils.utcnow()
        for port in ports:
            if not (port.get('description') or '').startswith(
                    neutron.PORT_OWNER_PREFIX):
                continue
            if port.get('device_id') in known:
                continue
            if port.get('status') != 'DOWN':
                # Still carrying traffic. Whatever the database thinks, that
                # is not an orphan.
                continue
            created = port.get('created_at')
            if created:
                try:
                    age = now - timeutils.parse_isotime(created).replace(
                        tzinfo=None)
                except (ValueError, TypeError):
                    continue
                if age.total_seconds() < CONF.compute.reclaim_orphan_ports_grace:
                    # A port made moments ago belongs to a container being
                    # created right now, whose row this may simply not have
                    # read yet.
                    continue
            LOG.info('Deleting orphan port %(port)s: it was made for '
                     '%(owner)s and its container %(device)s is gone',
                     {'port': port['id'], 'owner': port['description'],
                      'device': port.get('device_id')})
            try:
                neutron_api.delete_port(port['id'])
            except Exception as e:
                LOG.warning('Could not delete orphan port %(port)s: %(err)s',
                            {'port': port['id'], 'err': str(e)})

    @periodic_task.periodic_task(
        spacing=CONF.compute.reclaim_node_resources_interval)
    @context.set_context
    def reclaim_orphan_node_resources(self, ctx):
        """Release node resources whose volume mapping is gone.

        A volume leaves two things on the node: a mount, and for a block
        volume a mapped device. Both are supposed to go when it detaches, and
        both stay if the service dies in the wrong second -- and once the
        mapping row is gone, nothing will ever come looking for them again.

        They are not cosmetic. A mapped rbd image keeps a watcher, and Ceph
        refuses to delete an image that something is watching: the volume goes
        to "available", the delete is rolled back, and what an operator sees is
        a volume that cannot be removed with nothing holding it. Both shapes
        happened here -- a leftover NFS mount from a service that crashed
        mid-attach, and a mapped device that outlived its capsule by an hour.

        Only paths under this service's own volume directory are touched, and
        only when no mapping claims them. Anything else on the node was put
        there by someone else.
        """
        try:
            volume_dir = CONF.volume.volume_dir
            mappings = objects.VolumeMapping.list(ctx)
            live = {m.volume.uuid for m in mappings}
            cinder_ids = {m.cinder_volume_id for m in mappings
                          if m.cinder_volume_id}
        except Exception as e:
            # An unreadable database reads as "nothing is live", which would
            # unmount every volume this node is serving.
            LOG.warning('Orphan resource sweep skipped: could not list volume '
                        'mappings: %s', str(e))
            return

        self._reclaim_orphan_mounts(volume_dir, live)
        self._reclaim_orphan_rbd_devices(cinder_ids)

    def _reclaim_orphan_mounts(self, volume_dir, live):
        try:
            with open('/proc/mounts') as f:
                mounted = [line.split()[1] for line in f
                           if len(line.split()) > 1]
        except OSError as e:
            LOG.warning('Orphan mount sweep skipped: %s', str(e))
            return

        for path in mounted:
            if os.path.dirname(path) != volume_dir.rstrip('/'):
                continue
            if os.path.basename(path) in live:
                continue
            LOG.info('Unmounting %s: no volume mapping claims it', path)
            try:
                utils.execute('umount', path, run_as_root=True)
                if os.path.isdir(path):
                    os.rmdir(path)
            except Exception as e:
                LOG.warning('Could not unmount %(path)s: %(err)s',
                            {'path': path, 'err': e})

    def _reclaim_orphan_rbd_devices(self, cinder_ids):
        """Unmap rbd images no mapping claims.

        The image name carries the Cinder volume id, which is what ties a
        mapped device back to a mapping -- the mapping's own uuid never
        reaches the device.
        """
        try:
            out = utils.execute('rbd', 'showmapped', '--format', 'json',
                                run_as_root=True)[0]
            devices = jsonutils.loads(out or '[]')
        except Exception as e:
            # No rbd on this node, or no images mapped: both are the normal
            # case on a deployment that does not use Ceph.
            LOG.debug('Skipping rbd sweep: %s', str(e))
            return

        if isinstance(devices, dict):  # older rbd keys by index
            devices = list(devices.values())

        for dev in devices:
            image = dev.get('name') or ''
            device = dev.get('device')
            if not image.startswith('volume-') or not device:
                continue
            if image[len('volume-'):] in cinder_ids:
                continue
            LOG.info('Unmapping %(device)s (%(image)s): no volume mapping '
                     'claims it', {'device': device, 'image': image})
            try:
                utils.execute('rbd', 'unmap', device, run_as_root=True)
            except Exception as e:
                LOG.warning('Could not unmap %(device)s: %(err)s',
                            {'device': device, 'err': e})

    @periodic_task.periodic_task(
        spacing=CONF.compute.reclaim_allocations_interval)
    @context.set_context
    def reclaim_stale_allocations(self, ctx):
        """Give back placement allocations whose consumer no longer exists.

        A claim is written to placement before a container is built, and the
        create path gives it back on failure -- but only if this service is
        alive to do it. Killed at the wrong moment, or failing in a way an
        earlier release of this code did not catch, the allocation stays,
        keyed by a container uuid nothing will ever ask about again. Enough of
        them and the node reports full and is scheduled nothing, while running
        almost nothing.

        Fails closed in every uncertain direction: it acts only on this node's
        own resource provider, only on consumers absent from the database, and
        it skips the whole sweep if the database cannot answer -- deleting on
        an unreadable database would take live containers' resources away from
        them.

        It does not run at all when the host is shared with nova. There the
        resource provider carries nova's allocations too, and this cannot tell
        one of its own leaked consumers from a running instance: both are
        absent from zun's database, and deleting an instance's allocation is a
        far worse outcome than leaving a leak. Making it safe there needs
        allocations tagged at the point they are written (placement's
        consumer_type, microversion 1.38) so the sweep can recognise its own;
        until then a shared host is swept by the operator, not by this.
        """
        if CONF.compute.host_shared_with_nova:
            LOG.debug('Allocation reclaim does not run on a host shared with '
                      'nova: nova\'s allocations live on the same resource '
                      'provider and cannot be told apart from stale ones.')
            return

        rt = self._get_resource_tracker()
        rp_uuid = getattr(rt, 'rp_uuid', None)
        if not rp_uuid:
            return

        try:
            allocations = self.reportclient.\
                get_allocations_for_resource_provider(ctx, rp_uuid).allocations
        except Exception as e:
            LOG.warning("Allocation reclaim skipped: could not read "
                        "placement: %s", str(e))
            return
        if not allocations:
            return

        try:
            live = set()
            for c in objects.Container.list_by_host(ctx, self.host):
                live.add(c.uuid)
            for c in objects.Capsule.list_by_host(ctx, self.host):
                live.add(c.uuid)
                for inner in (c.containers or []):
                    live.add(inner.uuid)
        except Exception as e:
            # An unreadable database reads as "every consumer is stale", which
            # would delete the allocations of everything actually running.
            LOG.warning("Allocation reclaim skipped: could not list this "
                        "node's containers: %s", str(e))
            return

        for consumer in allocations:
            if consumer in live:
                continue
            LOG.info("Reclaiming placement allocation of %s: no such "
                     "container on this node", consumer)
            try:
                self.reportclient.delete_allocation_for_container(
                    ctx, consumer)
            except Exception as e:
                LOG.warning("Could not reclaim allocation %s: %s",
                            consumer, str(e))

    @periodic_task.periodic_task(spacing=CONF.sync_container_state_interval,
                                 run_immediately=True)
    @context.set_context
    def sync_container_state(self, ctx):
        LOG.debug('Start syncing container states.')

        capsules = objects.Capsule.list_by_host(ctx, self.host)
        if isinstance(self.driver, driver_module.ContainerDriver):
            containers = objects.Container.list_by_host(ctx, self.host)
            self.driver.update_containers_states(ctx, containers, self)
            # TODO(hongbin): use capsule driver to update capsules status
            self.driver.update_containers_states(ctx, capsules, self)
        elif hasattr(self.capsule_driver, 'update_containers_states'):
            self.capsule_driver.update_containers_states(ctx, capsules, self)
        else:
            # Capsule-only host whose capsule driver cannot report state yet:
            # capsule status stays as recorded at the end of the last
            # operation until the driver implements this.
            LOG.debug('State sync is not implemented by capsule driver %s',
                      type(self.capsule_driver).__name__)

    def network_detach(self, context, container, network):
        @utils.synchronized(container.uuid)
        def do_network_detach():
            self._do_network_detach(context, container, network)

        utils.spawn_n(do_network_detach)

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.NETWORK_DETACH)
    def _do_network_detach(self, context, container, network):
        LOG.debug('Detach network: %(network)s from container: %(container)s.',
                  {'container': container, 'network': network})
        with self._update_task_state(context, container,
                                     consts.NETWORK_DETACHING):
            # NOTE(hongbin): capsule shouldn't reach here
            self.driver.network_detach(context, container, network)

    def network_attach(self, context, container, requested_network):
        @utils.synchronized(container.uuid)
        def do_network_attach():
            self._do_network_attach(context, container, requested_network)

        utils.spawn_n(do_network_attach)

    @wrap_exception()
    @wrap_container_event(prefix='compute',
                          finish_action=container_actions.NETWORK_ATTACH)
    def _do_network_attach(self, context, container, requested_network):
        LOG.debug('Attach network: %(network)s to container: %(container)s.',
                  {'container': container, 'network': requested_network})
        with self._update_task_state(context, container,
                                     consts.NETWORK_ATTACHING):
            # NOTE(hongbin): capsule shouldn't reach here
            self.driver.network_attach(context, container, requested_network)

    def network_create(self, context, neutron_net_id):
        LOG.debug('Create network')
        return self.driver.create_network(context, neutron_net_id)

    def network_delete(self, context, network):
        LOG.debug('Delete network')
        self.driver.delete_network(context, network)

    def resize_container(self, context, container, patch):
        @utils.synchronized(container.uuid)
        def do_container_resize():
            self.container_update(context, container, patch)

        utils.spawn_n(do_container_resize)
