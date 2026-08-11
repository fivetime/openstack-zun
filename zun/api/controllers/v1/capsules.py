#    Copyright 2017 ARM Holdings.
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

import shlex

from neutronclient.common import exceptions as n_exc
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import strutils
import pecan

from zun.api.controllers import base
from zun.api.controllers import link
from zun.api.controllers.v1 import collection
from zun.api.controllers.v1.schemas import capsules as schema
from zun.api.controllers.v1.schemas import parameter_types
from zun.api.controllers.v1.views import capsules_view as view
from zun.api import utils as api_utils
from zun.api import validation
from zun.api.validation import validators
from zun.common import consts
from zun.common import exception
from zun.common.i18n import _
from zun.common import name_generator
from zun.common import policy
from zun.common import utils
import zun.conf
from zun.container import driver
from zun.network import neutron
from zun import objects
from zun.volume import cinder_api as cinder


CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)


def check_policy_on_capsule(capsule, action):
    context = pecan.request.context
    policy.enforce(context, action, capsule, action=action)


def _validate_security_context(sc):
    """Refuse the parts of a securityContext this platform will not grant.

    The rest of a securityContext only makes a container SAFER -- a non-root
    user, a read-only root filesystem, dropped capabilities, a seccomp profile
    -- and is honoured as written. Two parts can make it MORE powerful, and are
    bounded here rather than passed through:

      capabilities.add  A tenant that can add SYS_ADMIN, NET_ADMIN and the like
                        has, inside its Kata guest, most of what "privileged"
                        would grant -- and privileged is refused outright. Only
                        the capabilities in [container_driver] allowed_capabilities
                        (by default the single one PodSecurity "restricted"
                        permits) may be added. Dropping is never restricted.

      seccompProfile: Localhost  Names a profile FILE on the compute host. A
                        tenant cannot install one, so it has no legitimate use,
                        and the reference is an arbitrary host path -- exactly
                        what a capsule must not be able to name. Only
                        RuntimeDefault and Unconfined are accepted.
    """
    caps = (sc.get('capabilities') or {})
    add = caps.get('add') or []
    if add:
        allowed = {c.upper() for c in CONF.allowed_capabilities}
        asked = {str(c).upper() for c in add}
        forbidden = sorted(asked - allowed)
        if forbidden:
            raise exception.Invalid(
                "securityContext.capabilities.add may not include %s; this "
                "host allows adding only %s" % (
                    ', '.join(forbidden),
                    ', '.join(sorted(allowed)) or '(none)'))

    profile = sc.get('seccompProfile') or {}
    if profile.get('type') == 'Localhost':
        raise exception.Invalid(
            "securityContext.seccompProfile type Localhost is not supported: "
            "it names a profile file on the compute host, which a tenant "
            "cannot install; use RuntimeDefault")


def check_capsule_template(tpl):
    # TODO(kevinz): add volume spec check
    tpl_json = tpl
    if isinstance(tpl, str):
        try:
            tpl_json = jsonutils.loads(tpl)
        except Exception as e:
            raise exception.FailedParseStringToJson(e)

        validator = validators.SchemaValidator(
            parameter_types.capsule_template)
        validator.validate(tpl_json)

    kind_field = tpl_json.get('kind')
    if kind_field not in ['capsule', 'Capsule']:
        raise exception.InvalidCapsuleTemplate("kind fields need to be "
                                               "set as capsule or Capsule")

    spec_field = tpl_json.get('spec')
    if spec_field is None:
        raise exception.InvalidCapsuleTemplate("No Spec found")
    # Align the Capsule restartPolicy with container restart_policy
    # Also change the template filed name from Kubernetes type to OpenStack
    # type.
    if 'restartPolicy' in spec_field.keys():
        spec_field['restartPolicy'] = \
            utils.VALID_CAPSULE_RESTART_POLICY[spec_field['restartPolicy']]
        spec_field[utils.VALID_CAPSULE_FIELD['restartPolicy']] = \
            spec_field.pop('restartPolicy')
    if spec_field.get('containers') is None:
        raise exception.InvalidCapsuleTemplate("No valid containers field")
    return spec_field, tpl_json


class CapsuleCollection(collection.Collection):
    """API representation of a collection of Capsules."""

    fields = {
        'capsules',
        'next'
    }

    """A list containing capsules objects"""

    def __init__(self, **kwargs):
        self._type = 'capsules'

    @staticmethod
    def convert_with_links(rpc_capsules, limit, url=None,
                           expand=False, legacy_api_version=False, **kwargs):
        context = pecan.request.context
        collection = CapsuleCollection()
        collection.capsules = \
            [view.format_capsule(url, p, context,
                                 legacy_api_version=legacy_api_version)
             for p in rpc_capsules]
        collection.next = collection.get_next(limit, url=url, **kwargs)
        return collection


class CapsuleController(base.Controller):
    """Controller for Capsules"""

    _custom_actions = {
        'logs': ['GET'],
        'execute': ['POST'],
        'stats': ['GET'],
        'update_file': ['POST'],
    }

    @base.Controller.api_version("1.1", "1.31")
    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_all(self, **kwargs):
        '''Retrieve a list of capsules.'''
        return self._do_get_all(legacy_api_version=True, **kwargs)

    @base.Controller.api_version("1.32")  # noqa
    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_all(self, **kwargs):  # noqa
        '''Retrieve a list of capsules.'''
        return self._do_get_all(**kwargs)

    def _do_get_all(self, legacy_api_version=False, **kwargs):
        context = pecan.request.context
        policy.enforce(context, "capsule:get_all",
                       action="capsule:get_all")
        if utils.is_all_projects(kwargs):
            context.all_projects = True
        limit = api_utils.validate_limit(kwargs.get('limit'))
        sort_dir = api_utils.validate_sort_dir(kwargs.get('sort_dir', 'asc'))
        sort_key = kwargs.get('sort_key', 'id')
        resource_url = kwargs.get('resource_url')
        expand = kwargs.get('expand')
        filters = None
        marker_obj = None
        marker = kwargs.get('marker')
        if marker:
            marker_obj = objects.Capsule.get_by_uuid(context,
                                                     marker)
        capsules = objects.Capsule.list(context,
                                        limit,
                                        marker_obj,
                                        sort_key,
                                        sort_dir,
                                        filters=filters)

        return CapsuleCollection.convert_with_links(
            capsules, limit, url=resource_url, expand=expand,
            sort_key=sort_key, sort_dir=sort_dir,
            legacy_api_version=legacy_api_version)

    @base.Controller.api_version("1.1", "1.31")
    @pecan.expose('json')
    @api_utils.enforce_content_types(['application/json'])
    @exception.wrap_pecan_controller_exception
    @validation.validated(schema.capsule_create)
    def post(self, **capsule_dict):
        """Create a new capsule.

        :param capsule_dict: a capsule within the request body.
        """
        return self._do_post(legacy_api_version=True, **capsule_dict)

    @base.Controller.api_version("1.32")  # noqa
    @pecan.expose('json')
    @api_utils.enforce_content_types(['application/json'])
    @exception.wrap_pecan_controller_exception
    @validation.validated(schema.capsule_create)
    def post(self, **capsule_dict):  # noqa
        """Create a new capsule.

        :param capsule_dict: a capsule within the request body.
        """
        return self._do_post(**capsule_dict)

    @staticmethod
    def _resolve_security_groups(context, names):
        """Turn the groups a capsule asked for into ids, or refuse.

        The driver accepts either form (common/utils.py:315-329), but names are
        resolved here so an unknown one is answered synchronously, by name, to
        whoever wrote it -- rather than reaching a compute node and failing
        there, where the tenant sees a capsule that died for no stated reason.

        find_resourceid_by_name_or_id is scoped to the caller's own project, so
        a tenant cannot borrow another project's group by naming it.
        """
        if names is None:
            return None
        if not names:
            # ⚠️ An empty list is not the same as no list, and the difference is
            # the difference between a port that allows nothing and one Neutron
            # hands the project's permissive default to. Saying "no groups"
            # explicitly must survive to the port, so it is returned as an empty
            # list rather than collapsed into None.
            return []
        neutron_api = neutron.NeutronAPI(context)
        resolved = []
        for name in sorted(set(names)):
            try:
                resolved.append(neutron_api.find_resourceid_by_name_or_id(
                    'security_group', name, context.project_id))
            except n_exc.NeutronClientNoUniqueMatch:
                raise exception.Conflict(_(
                    'Multiple security groups are named %(name)s; use an id '
                    'to say which.') % {'name': name})
            except n_exc.NeutronClientException as e:
                if e.status_code == 404:
                    raise exception.InvalidValue(_(
                        'Security group %(name)s not found.')
                        % {'name': name})
                raise
        return resolved

    def _do_post(self, legacy_api_version=False, **capsule_dict):
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        policy.enforce(context, "capsule:create",
                       action="capsule:create")

        # Abstract the capsule specification
        capsules_template = capsule_dict.get('template')

        spec_content, template_json = \
            check_capsule_template(capsules_template)

        containers_spec, init_containers_spec = \
            utils.capsule_get_container_spec(spec_content)
        volumes_spec = utils.capsule_get_volume_spec(spec_content)

        # Create the capsule Object
        new_capsule = objects.Capsule(context, **capsule_dict)
        new_capsule.project_id = context.project_id
        new_capsule.user_id = context.user_id
        new_capsule.status = consts.CREATING
        new_capsule.create(context)
        new_capsule.volumes = []
        capsule_need_cpu = 0
        capsule_need_memory = 0
        container_volume_requests = []

        if spec_content.get('restart_policy'):
            capsule_restart_policy = spec_content.get('restart_policy')
        else:
            # NOTE(hongbin): this is deprecated but we need to maintain
            # backward-compatibility. Will remove this branch in the future.
            capsule_restart_policy = template_json.get('restart_policy',
                                                       'always')
        container_restart_policy = {"MaximumRetryCount": "0",
                                    "Name": capsule_restart_policy}
        new_capsule.restart_policy = container_restart_policy

        metadata_info = template_json.get('metadata', None)
        # Resolved here rather than at attach time so a name that does not
        # exist is a synchronous error naming the group, instead of a capsule
        # that reaches a compute node and dies there. Scoped to the caller's
        # own project by find_resourceid_by_name_or_id, so a tenant cannot
        # name a group belonging to anybody else.
        new_capsule.security_groups = self._resolve_security_groups(
            context, template_json.get('securityGroups'))

        requested_networks_info = template_json.get('nets', [])
        requested_networks = \
            utils.build_requested_networks(context, requested_networks_info)

        if metadata_info:
            new_capsule.name = metadata_info.get('name', None)
            new_capsule.labels = metadata_info.get('labels', None)
            new_capsule.annotations = metadata_info.get('annotations', None)

        # create the capsule in DB so that it generates a 'id'
        new_capsule.save()

        extra_spec = {}
        az_info = template_json.get('availabilityZone')
        if az_info:
            extra_spec['availability_zone'] = az_info

        # An image built for one architecture cannot run on another, and
        # nothing downstream would catch it: the capsule would be placed
        # anywhere and only fail when the container tried to execute. Asking
        # Placement for the matching architecture trait refuses the wrong host
        # while it is still a scheduling decision.
        arch = template_json.get('architecture')
        if arch:
            arch = driver.ARCH_ALIASES.get(arch.lower(), arch.lower())
            trait = driver.ARCH_TRAITS.get(arch)
            if not trait:
                raise exception.InvalidCapsuleTemplate(
                    "unsupported architecture %s" % arch)
            extra_spec['trait:%s' % trait] = 'required' 

        new_capsule.image = CONF.sandbox_image
        new_capsule.image_driver = CONF.sandbox_image_driver

        # calculate capsule cpu/ram
        # 1. sum all cpu/ram of regular containers
        for container_dict in containers_spec:
            if not container_dict.get('resources'):
                continue
            allocation = container_dict['resources']['requests']
            if allocation.get('cpu'):
                capsule_need_cpu += allocation['cpu']
            if allocation.get('memory'):
                capsule_need_memory += allocation['memory']
        # 2. take the maximum of each init container
        for container_dict in init_containers_spec:
            if not container_dict.get('resources'):
                continue
            allocation = container_dict['resources']['requests']
            if allocation.get('cpu'):
                capsule_need_cpu = max(capsule_need_cpu, allocation['cpu'])
            if allocation.get('memory'):
                capsule_need_memory = max(capsule_need_memory,
                                          allocation['memory'])

        merged_containers_spec = init_containers_spec + containers_spec
        for container_spec in merged_containers_spec:
            if container_spec.get('image_pull_policy'):
                if not policy.enforce(
                        context, "container:create:image_pull_policy",
                        action="container:create:image_pull_policy",
                        do_raise=False):
                    LOG.info("Policy doesn't support image_pull_policy")
                    container_spec.pop('image_pull_policy')
            container_dict = container_spec
            container_dict['project_id'] = context.project_id
            container_dict['user_id'] = context.user_id
            name = self._generate_name_for_capsule_container(new_capsule)
            container_dict['name'] = name

            if container_dict.get('args') and container_dict.get('command'):
                container_dict['command'] = \
                    container_dict['command'] + container_dict['args']
                container_dict.pop('args')
            elif container_dict.get('args'):
                container_dict['command'] = container_dict['args']
                container_dict.pop('args')

            if container_dict.get('ports'):
                exposed_ports = {}
                ports = container_dict.pop('ports')
                for port in ports:
                    container_port = "%s/%s" % (
                        port['containerPort'],
                        port.get('protocol', 'tcp').lower())
                    host_port = {}
                    exposed_ports[container_port] = host_port
                container_dict['exposed_ports'] = exposed_ports

            if container_dict.get('resources'):
                resources_list = container_dict.get('resources')
                allocation = resources_list.get('requests')
                if allocation.get('cpu'):
                    container_dict['cpu'] = allocation.get('cpu')
                if allocation.get('memory'):
                    container_dict['memory'] = str(allocation['memory'])
                container_dict.pop('resources')

            # Probes ride in the existing healthcheck column rather than a
            # new one: adding a column means a migration and an object version
            # bump, and nothing else writes this key. The docker driver reads
            # its own keys ('test', 'interval', ...) and is unaffected.
            probes = {}
            for field in ('livenessProbe', 'readinessProbe', 'startupProbe'):
                probe = container_dict.pop(field, None)
                if probe:
                    probes[field] = probe
            if probes:
                healthcheck = container_dict.get('healthcheck') or {}
                healthcheck['k8s_probes'] = probes
                container_dict['healthcheck'] = healthcheck

            # The security context rides the same column, for the same reason,
            # and per container because that is where Kubernetes puts it.
            #
            # ⚠️ It cannot travel in the capsule's annotations keyed by container
            # name, which was the first attempt: the name in the spec is
            # overwritten a few lines above with a generated one, so by the time
            # the driver looks, the key it was given no longer names anything.
            # The container ran as root with a writable root filesystem and
            # nothing reported it, which is the failure this whole field exists
            # to prevent.
            security_context = container_dict.pop('securityContext', None)
            if security_context:
                _validate_security_context(security_context)
                healthcheck = container_dict.get('healthcheck') or {}
                healthcheck['k8s_security_context'] = security_context
                container_dict['healthcheck'] = healthcheck

            container_dict['image_pull_policy'] = (
                container_dict.get('image_pull_policy', 'always').lower())
            container_dict['status'] = consts.CREATING
            container_dict['capsule_id'] = new_capsule.id
            container_dict['restart_policy'] = container_restart_policy
            if container_spec in init_containers_spec:
                if capsule_restart_policy == "always":
                    container_restart_policy = {"MaximumRetryCount": "10",
                                                "Name": "on-failure"}
                    container_dict['restart_policy'] = container_restart_policy
                utils.check_for_restart_policy(container_dict)
                new_container = objects.CapsuleInitContainer(context,
                                                             **container_dict)
            else:
                utils.check_for_restart_policy(container_dict)
                new_container = objects.CapsuleContainer(context,
                                                         **container_dict)
            new_container.create(context)

            if container_dict.get('volumeMounts'):
                for volume in container_dict['volumeMounts']:
                    volume['container_uuid'] = new_container.uuid
                    container_volume_requests.append(volume)

        # Deal with the volume support
        requested_volumes = \
            self._build_requested_volumes(context,
                                          volumes_spec,
                                          container_volume_requests,
                                          new_capsule)
        new_capsule.cpu = capsule_need_cpu
        new_capsule.memory = str(capsule_need_memory)
        new_capsule.save(context)

        kwargs = {}
        kwargs['extra_spec'] = extra_spec
        kwargs['requested_networks'] = requested_networks
        kwargs['requested_volumes'] = requested_volumes
        kwargs['run'] = False
        compute_api.container_create(context, new_capsule, **kwargs)
        # Set the HTTP Location Header
        pecan.response.location = link.build_url('capsules',
                                                 new_capsule.uuid)

        pecan.response.status = 202
        return view.format_capsule(pecan.request.host_url, new_capsule,
                                   context,
                                   legacy_api_version=legacy_api_version)

    @base.Controller.api_version("1.1", "1.31")
    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_one(self, capsule_ident):
        """Retrieve information about the given capsule.

        :param capsule_ident: UUID or name of a capsule.
        """
        return self._do_get_one(capsule_ident, legacy_api_version=True)

    @base.Controller.api_version("1.32")  # noqa
    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_one(self, capsule_ident):  # noqa
        """Retrieve information about the given capsule.

        :param capsule_ident: UUID or name of a capsule.
        """
        return self._do_get_one(capsule_ident)

    def _do_get_one(self, capsule_ident, legacy_api_version=False):
        context = pecan.request.context
        capsule = api_utils.get_resource('Capsule', capsule_ident)
        check_policy_on_capsule(capsule.as_dict(), "capsule:get")
        return view.format_capsule(pecan.request.host_url, capsule, context,
                                   legacy_api_version=legacy_api_version)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def logs(self, capsule_ident, container=None, stdout=True, stderr=True,
             timestamps=False, tail='all', since=None):
        """Get the logs of a container in the given capsule.

        A capsule holds several containers, so which one is being asked for has
        to be named; without it the only sensible answer would be the first,
        and a caller that meant another would silently read the wrong one.

        :param capsule_ident: UUID or Name of a capsule.
        :param container: Name or UUID of a container within the capsule.
        :param stdout: Get standard output if True.
        :param stderr: Get standard error if True.
        :param timestamps: Prefix every line with its timestamp.
        :param tail: Number of lines to show from the end of the logs.
        :param since: Show logs since an epoch second or ISO 8601 time.
        """
        capsule = api_utils.get_resource('Capsule', capsule_ident)
        check_policy_on_capsule(capsule.as_dict(), "capsule:logs")

        target = self._find_container(capsule, container)
        try:
            stdout = strutils.bool_from_string(stdout, strict=True)
            stderr = strutils.bool_from_string(stderr, strict=True)
            timestamps = strutils.bool_from_string(timestamps, strict=True)
        except ValueError:
            bools = ', '.join(strutils.TRUE_STRINGS + strutils.FALSE_STRINGS)
            raise exception.InvalidValue(_('Valid stdout, stderr and '
                                           'timestamps values are: %s')
                                         % bools)

        if not capsule.host:
            # Not placed yet, so there is no node holding a log file. Saying so
            # beats an empty answer, which reads as "ran and printed nothing".
            raise exception.Invalid(
                _('Capsule is not running on any host yet'))
        # Only the capsule records its host; its containers do not. Without
        # this the RPC goes to the shared topic and any compute node answers —
        # and every node but the right one has no log file, so the call returns
        # empty roughly as often as the deployment has nodes.
        target.host = capsule.host

        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return compute_api.container_logs(context, target, stdout, stderr,
                                          timestamps, tail, since)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def update_file(self, capsule_ident, path=None, contents=None):
        """Replace the contents of one of the capsule's file volumes.

        For content that has to change while the capsule runs, which a capsule
        otherwise has no way to express: it is built once and cannot be
        changed, and the only alternatives are recreating it -- restarting the
        workload and losing its address -- or running a command inside the
        container, which needs a shell the better images do not have.

        The file is rewritten where it is, so a container reading it sees the
        new contents without anything being remounted.

        :param capsule_ident: UUID or Name of a capsule.
        :param path: the path the volume is mounted at inside the container.
        :param contents: the new contents, base64 encoded.
        """
        capsule = api_utils.get_resource('Capsule', capsule_ident)
        check_policy_on_capsule(capsule.as_dict(), "capsule:update_file")

        if not path or contents is None:
            raise exception.Invalid(
                _('Both path and contents are required'))
        if not capsule.host:
            raise exception.Invalid(
                _('Capsule is not running on any host yet'))

        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.capsule_update_file(context, capsule, path, contents)
        return {'updated': path}

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def stats(self, capsule_ident):
        """Resource usage of every container in the given capsule.

        Per-container rather than one figure for the capsule: that is the shape
        the runtime accounts in, and a caller driving autoscaling or answering
        "which container is using the memory" needs the breakdown. Summing is
        cheap for a caller that wants the total; splitting a total is not
        possible.

        CPU is a cumulative nanosecond count, not a rate, and is returned as
        the runtime reports it together with the timestamp it was read at. Two
        readings make a rate; one does not, and a rate invented here would be
        wrong across a container restart.

        :param capsule_ident: UUID or Name of a capsule.
        """
        capsule = api_utils.get_resource('Capsule', capsule_ident)
        check_policy_on_capsule(capsule.as_dict(), "capsule:stats")

        if not capsule.host:
            raise exception.Invalid(
                _('Capsule is not running on any host yet'))

        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return {'stats': compute_api.capsule_stats(context, capsule)}

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def execute(self, capsule_ident, container=None, run=True,
                interactive=False, **kwargs):
        """Run a command in a container of the given capsule.

        :param capsule_ident: UUID or Name of a capsule.
        :param container: Name or UUID of a container within the capsule.
        :param run: Run the command immediately and return its output.
        :param interactive: Keep stdin open and allocate a terminal. The
            answer is then a proxy url to attach to rather than output: there
            is no output yet, and there will not be until someone types.
        """
        capsule = api_utils.get_resource('Capsule', capsule_ident)
        check_policy_on_capsule(capsule.as_dict(), "capsule:execute")
        utils.validate_container_state(capsule, 'execute')

        target = self._find_container(capsule, container)
        command = kwargs.get('command')
        if not command:
            raise exception.Invalid(_('command is required'))
        if isinstance(command, str):
            command = shlex.split(command)

        try:
            run = strutils.bool_from_string(run, strict=True)
            interactive = strutils.bool_from_string(interactive, strict=True)
        except ValueError:
            bools = ', '.join(strutils.TRUE_STRINGS + strutils.FALSE_STRINGS)
            raise exception.InvalidValue(
                _('Valid run and interactive values are: %s') % bools)
        if run and interactive:
            raise exception.Invalid(_(
                'run and interactive are mutually exclusive: one waits for '
                'the command to finish, the other hands back a session to '
                'type into'))

        if not capsule.host:
            raise exception.Invalid(
                _('Capsule is not running on any host yet'))
        # Same reason as logs: only the capsule records its host, and an
        # undirected call would run the command on whichever node answered.
        target.host = capsule.host

        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return compute_api.container_exec(context, target, command, run,
                                          interactive)

    @staticmethod
    def _find_container(capsule, ident):
        containers = capsule.containers + capsule.init_containers
        if not containers:
            raise exception.Invalid(_('Capsule has no containers'))
        if ident is None:
            if len(containers) > 1:
                raise exception.Invalid(
                    _('Capsule has more than one container; name the one to '
                      'read with the container parameter'))
            return containers[0]
        for c in containers:
            if ident in (c.name, c.uuid):
                return c
        raise exception.ContainerNotFound(container=ident)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def delete(self, capsule_ident, **kwargs):
        """Delete a capsule.

        :param capsule_ident: UUID or Name of a capsule.
        """
        context = pecan.request.context
        if utils.is_all_projects(kwargs):
            policy.enforce(context, "capsule:delete_all_projects",
                           action="capsule:delete_all_projects")
            context.all_projects = True
        capsule = api_utils.get_resource('Capsule', capsule_ident)
        check_policy_on_capsule(capsule.as_dict(), "capsule:delete")
        compute_api = pecan.request.compute_api
        capsule.task_state = consts.CONTAINER_DELETING
        capsule.save(context)
        if capsule.host:
            compute_api.container_delete(context, capsule)
        else:
            merged_containers = capsule.containers + capsule.init_containers
            for container in merged_containers:
                container.destroy(context)
            capsule.destroy(context)
        pecan.response.status = 204

    def _generate_name_for_capsule_container(self, new_capsule):
        """Generate a random name like: zeta-22-container."""
        name_gen = name_generator.NameGenerator()
        name = name_gen.generate()
        if new_capsule.name is None:
            return 'capsule-' + new_capsule.uuid + '-' + name
        else:
            return 'capsule-' + new_capsule.name + '-' + name

    def _build_requested_volumes(self, context, volume_spec,
                                 volume_mounts, capsule):
        # NOTE(kevinz): We assume the volume_mounts has been pretreated,
        # there won't occur that volume multiple attach and no untapped
        # volume.
        cinder_api = cinder.CinderAPI(context)
        requested_volumes = {}
        volume_created = []
        try:
            for mount in volume_spec:
                auto_remove = False
                contents = None
                volume = None
                if 'file' in mount:
                    # A file carried with the capsule: its content is written
                    # to the compute node and bind-mounted in, so a container
                    # can read configuration from disk rather than only from
                    # its environment.
                    volume_driver = 'local'
                    contents = mount['file']['contents']
                    auto_remove = True
                elif 'nfs' in mount:
                    # The provisioner already made the share; the node mounts
                    # it and, given the share id, grants itself access with
                    # the request's own token.
                    volume_driver = 'nfs'
                    contents = jsonutils.dumps(mount['nfs'])
                    auto_remove = True
                elif 'emptyDir' in mount:
                    # Scratch space for the capsule, shared by whichever of its
                    # containers mount it and gone when it is. There is nothing
                    # to store and nothing to attach: the driver makes the
                    # directory on whichever node the capsule lands on.
                    #
                    # The options ride along in `contents` because that is the
                    # field a volume already has for something that is not a
                    # Cinder id. It is not file content and is not treated as
                    # any.
                    volume_driver = 'emptydir'
                    contents = jsonutils.dumps(mount['emptyDir'] or {})
                    auto_remove = True
                else:
                    volume_driver = 'cinder'
                    mount_driver = mount[volume_driver]
                    if mount_driver.get('fsGroup'):
                        # Ownership the volume is given after mounting, so the
                        # pod's user can actually write to it. Rides in
                        # `contents`, the field a volume has for what is not a
                        # Cinder id.
                        contents = jsonutils.dumps(
                            {'fsGroup': mount_driver['fsGroup']})
                    if mount_driver.get("volumeID"):
                        uuid = mount_driver.get("volumeID")
                        volume = cinder_api.search_volume(uuid)
                        cinder_api.ensure_volume_usable(volume)
                    else:
                        size = mount_driver.get("size")
                        volume = cinder_api.create_volume(size)
                        volume_created.append(volume)
                        if "autoRemove" in mount_driver.keys() \
                                and mount_driver.get("autoRemove", False):
                            auto_remove = True

                mount_destination = None
                container_uuid = None

                volume_object = objects.Volume(
                    context,
                    cinder_volume_id=volume.id if volume else None,
                    volume_provider=volume_driver,
                    contents=contents,
                    user_id=context.user_id,
                    project_id=context.project_id,
                    auto_remove=auto_remove)
                volume_object.create(context)

                for item in volume_mounts:
                    if item['name'] == mount['name']:
                        mount_destination = item['mountPath']
                        container_uuid = item['container_uuid']
                        volmapp = objects.VolumeMapping(
                            context,
                            container_path=mount_destination,
                            contents=contents,
                            volume_provider=volume_driver,
                            user_id=context.user_id,
                            project_id=context.project_id,
                            volume_id=volume_object.id)
                        requested_volumes.setdefault(container_uuid, [])
                        requested_volumes[container_uuid].append(volmapp)

                if not mount_destination or not container_uuid:
                    msg = _("volume mount parameters is invalid.")
                    raise exception.Invalid(msg)
        except Exception as e:
            # if volume search or created failed, will remove all
            # the created volume. The existed volume will remain.
            for volume in volume_created:
                try:
                    cinder_api.delete_volume(volume.id)
                except Exception as exc:
                    LOG.error('Error on deleting volume "%s": %s.',
                              volume.id, str(exc))

            # Since the container and capsule database model has been created,
            # we need to delete them here due to the volume create failed.
            for container in capsule.containers:
                try:
                    container.destroy(context)
                except Exception as exc:
                    LOG.warning('fail to delete the container %s: %s',
                                container.uuid, exc)

            capsule.destroy(context)

            raise e

        return requested_volumes
