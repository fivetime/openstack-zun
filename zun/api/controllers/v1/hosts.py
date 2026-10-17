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

import pecan

from zun.api.controllers import base
from zun.api.controllers.v1 import collection
from zun.api.controllers.v1.views import hosts_view as view
from zun.api import utils as api_utils
from zun.common import exception
from zun.common.i18n import _
from zun.common import policy
from zun import objects


def check_policy_on_host(host, action):
    context = pecan.request.context
    policy.enforce(context, action, host, action=action)


class HostCollection(collection.Collection):
    """API representation of a collection of hosts."""

    fields = {
        'hosts',
        'next'
    }

    """A list containing compute node objects"""

    def __init__(self, **kwargs):
        super(HostCollection, self).__init__(**kwargs)
        self._type = 'hosts'

    @staticmethod
    def convert_with_links(nodes, limit, url=None,
                           expand=False, **kwargs):
        collection = HostCollection()
        collection.hosts = [view.format_host(url, p) for p in nodes]
        collection.next = collection.get_next(limit, url=url, **kwargs)
        return collection


class HostController(base.Controller):
    """Host info controller"""

    _custom_actions = {
        'stats': ['GET'],
    }

    @pecan.expose('json')
    @base.Controller.api_version("1.4")
    @exception.wrap_pecan_controller_exception
    def get_all(self, **kwargs):
        """Retrieve a list of hosts"""
        context = pecan.request.context
        policy.enforce(context, "host:get_all",
                       action="host:get_all")
        return self._get_host_collection(**kwargs)

    def _get_host_collection(self, **kwargs):
        context = pecan.request.context
        limit = api_utils.validate_limit(kwargs.get('limit'))
        sort_dir = api_utils.validate_sort_dir(kwargs.get('sort_dir', 'asc'))
        sort_key = kwargs.get('sort_key', 'hostname')
        expand = kwargs.get('expand')
        filters = None
        marker_obj = None
        resource_url = kwargs.get('resource_url')
        marker = kwargs.get('marker')
        if marker:
            marker_obj = objects.ComputeNode.get_by_uuid(context, marker)
        nodes = objects.ComputeNode.list(context,
                                         limit,
                                         marker_obj,
                                         sort_key,
                                         sort_dir,
                                         filters=filters)
        return HostCollection.convert_with_links(nodes, limit,
                                                 url=resource_url,
                                                 expand=expand,
                                                 sort_key=sort_key,
                                                 sort_dir=sort_dir)

    @pecan.expose('json')
    @base.Controller.api_version("1.4")
    @exception.wrap_pecan_controller_exception
    def get_one(self, host_ident):
        """Retrieve information about the given host.

        :param host_ident: UUID or name of a host.
        """
        context = pecan.request.context
        policy.enforce(context, "host:get", action="host:get")
        host = api_utils.get_resource('ComputeNode', host_ident)
        return view.format_host(pecan.request.host_url, host)

    @pecan.expose('json')
    @base.Controller.api_version("1.56")
    @exception.wrap_pecan_controller_exception
    def stats(self, host_ident, **kwargs):
        """A reading of every container and capsule on a host, taken now.

        For a metering collector polling hosts rather than containers:
        one call per host, however many it runs. zun keeps no schedule of
        its own -- the caller decides how often. Each object comes with
        what a reader needs to account for it: owner, allocation, type,
        and for a capsule's container the capsule it belongs to (whose
        allocation already includes it).

        :param host_ident: UUID or name of a host.
        :param type: comma-separated kinds to read -- container, capsule,
            capsule_container (its init containers included). Default
            container,capsule.
        :param label: only objects carrying this label (`key` or
            `key=value`).
        :param interval: read twice this many seconds apart (1-5).
        """
        context = pecan.request.context
        policy.enforce(context, "host:stats", action="host:stats")
        host = api_utils.get_resource('ComputeNode', host_ident)
        types = [t.strip() for t in
                 str(kwargs.pop('type', 'container,capsule')).split(',')
                 if t.strip()]
        unknown = set(types) - {'container', 'capsule', 'capsule_container'}
        if unknown or not types:
            raise exception.InvalidValue(
                _('type must be some of container, capsule, '
                  'capsule_container'))
        label = kwargs.pop('label', None)
        interval = kwargs.pop('interval', 0)
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = -1
        if not 0 <= interval <= 5:
            raise exception.InvalidValue(_('interval must be 0 to 5'))
        if kwargs:
            raise exception.InvalidValue(
                _('Unknown parameters: %s') % ', '.join(sorted(kwargs)))
        compute_api = pecan.request.compute_api
        answer = compute_api.host_sample(
            context, host.hostname, types=types, label=label,
            interval=interval)
        answer['host'] = host.hostname
        return answer
