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

import itertools

from zun.api.controllers import link
from zun.common.policies import container as policies

_basic_keys = (
    'uuid',
    'user_id',
    'project_id',
    'name',
    'image',
    'links',
    'command',
    'status',
    'status_reason',
    'task_state',
    'cpu',
    'memory',
    'environment',
    'workdir',
    'ports',
    'hostname',
    'labels',
    'addresses',
    'image_pull_policy',
    'host',
    'restart_policy',
    'status_detail',
    'exit_code',
    'health',
    'dns',
    'dns_search',
    'interactive',
    'tty',
    'image_driver',
    'security_groups',
    'auto_remove',
    'runtime',
    'disk',
    'pids_limit',
    'swap',
    'blkio_weight',
    'device_read_bps',
    'device_write_bps',
    'device_read_iops',
    'device_write_iops',
    'auto_heal',
    'privileged',
    'healthcheck',
    'cpu_policy',
    'registry_id',
    'entrypoint',
    'created_at',
    'updated_at',
    'started_at',
    'size_rw',
    'size_measured_at',
)


def format_container(context, url, container, usage=None):
    """One container as the API shows it.

    `usage` is what the API last heard about this container from the node
    running it -- a measurement, held in a cache, never in the database.
    Absent, the size fields read as null: unknown, which is distinct from
    zero and is the honest answer for a node that has not reported.
    """
    def transform(key, value):
        if key not in _basic_keys:
            return
        # strip the key if it is not allowed by policy
        policy_action = policies.CONTAINER % ('get_one:%s' % key)
        if not context.can(policy_action, fatal=False, might_not_exist=True):
            return
        if key == 'uuid':
            yield ('uuid', value)
            if url:
                yield ('links', [link.make_link(
                    'self', url, 'containers', value),
                    link.make_link(
                        'bookmark', url,
                        'containers', value,
                        bookmark=True)])
        elif key == 'registry_id':
            if value:
                # the value is an internal id so replace it with the
                # user-facing uuid
                value = container.registry.uuid
            yield ('registry_id', value)
        else:
            yield (key, value)

    fields = container.as_dict()
    usage = usage or {}
    fields['size_rw'] = usage.get('size_rw')
    fields['size_measured_at'] = usage.get('measured_at')
    return dict(itertools.chain.from_iterable(
        transform(k, v) for k, v in fields.items()))
