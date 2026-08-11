# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""The few Manila calls an NFS volume needs, on the request's own session.

Raw REST rather than python-manilaclient, deliberately: the dependency would
have to reach every compute node for four calls, and the calls are the easy
kind -- one action endpoint, polled once.

Everything here runs with the request context's token. That is the tenant's
own credential, which is what makes the grant model work: a node can only
manage access to shares of the tenant whose capsule it is placing, because
that is whose token it is holding.
"""

import time

from oslo_log import log as logging

from zun.common import clients
from zun.common import exception
from zun.common.i18n import _

LOG = logging.getLogger(__name__)

# The oldest microversion that carries everything used here. Access rules and
# their state predate it comfortably.
_API_VERSION = '2.14'


def _session(context):
    return clients.OpenStackClients(context).keystone().session


def _endpoint(context):
    sess = _session(context)
    for service_type in ('sharev2', 'shared-file-system'):
        try:
            ep = sess.get_endpoint(service_type=service_type,
                                   interface='public')
            if ep:
                return ep.rstrip('/')
        except Exception:
            continue
    raise exception.ZunException(_(
        'this deployment has no shared filesystem endpoint'))


def _action(context, share_id, body, retries=5):
    """One share action, retried while manila is busy applying rules.

    Two nodes granting themselves access to the same share at the same time is
    the normal case, not a corner: a ReadWriteMany claim exists to be mounted
    from several places at once, and every one of those mounts starts with a
    grant. Manila applies rules one at a time and answers 400 to the second
    request while the first is in flight, so the second asks again rather than
    failing a pod over winning second place in a race.
    """
    sess = _session(context)
    url = '%s/shares/%s/action' % (_endpoint(context), share_id)
    delay = 2
    for attempt in range(retries):
        resp = sess.post(url, json=body, raise_exc=False,
                         headers={'X-OpenStack-Manila-API-Version':
                                  _API_VERSION})
        if resp.status_code < 400:
            return resp
        if resp.status_code == 400 and attempt < retries - 1:
            LOG.debug('manila is busy (%(status)s) for %(action)s on '
                      '%(share)s; asking again',
                      {'status': resp.status_code, 'action': list(body)[0],
                       'share': share_id})
            time.sleep(delay)
            delay = min(delay * 2, 15)
            continue
        raise exception.ZunException(_(
            'manila refused %(action)s on share %(share)s: %(status)s %(text)s')
            % {'action': list(body)[0], 'share': share_id,
               'status': resp.status_code, 'text': resp.text[:300]})


def access_rules(context, share_id):
    """The share's access rules, each {'id', 'access_to', 'state', ...}."""
    resp = _action(context, share_id, {'access_list': None})
    return resp.json().get('access_list', [])


def grant(context, share_id, client_ip, timeout=60):
    """Let one address mount the share, and wait until the rule is live.

    A single /32, never a subnet: the grant set is the isolation boundary a
    host-mounted NFS share has, so it must never be wider than the nodes
    actually running a pod that mounts it.

    Granting an address that already has a rule is not an error -- a second
    capsule of the same tenant on the same node arrives here too.
    """
    for rule in access_rules(context, share_id):
        if rule.get('access_to') == client_ip:
            _wait_active(context, share_id, client_ip, timeout)
            return
    _action(context, share_id, {'allow_access': {
        'access_type': 'ip', 'access_to': client_ip, 'access_level': 'rw'}})
    _wait_active(context, share_id, client_ip, timeout)


def _wait_active(context, share_id, client_ip, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for rule in access_rules(context, share_id):
            if rule.get('access_to') != client_ip:
                continue
            state = rule.get('state')
            if state == 'active':
                return
            if state == 'error':
                raise exception.ZunException(_(
                    'the access rule for %(ip)s on share %(share)s failed')
                    % {'ip': client_ip, 'share': share_id})
        time.sleep(2)
    raise exception.ZunException(_(
        'the access rule for %(ip)s on share %(share)s did not become '
        'active within %(t)ss')
        % {'ip': client_ip, 'share': share_id, 't': timeout})


def revoke(context, share_id, client_ip):
    """Withdraw an address's access. Quiet when there was none."""
    for rule in access_rules(context, share_id):
        if rule.get('access_to') == client_ip:
            try:
                _action(context, share_id,
                        {'deny_access': {'access_id': rule['id']}})
            except exception.ZunException as e:
                # Best effort: the mount is already gone, and a rule left
                # behind is a smaller wrong than a detach that cannot finish.
                LOG.warning('could not revoke %(ip)s on share %(share)s: '
                            '%(err)s',
                            {'ip': client_ip, 'share': share_id, 'err': e})
