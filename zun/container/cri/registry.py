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

"""Sending a committed image to the registry its name points at.

The docker driver hands this to the docker daemon. There is none on a
node that runs containerd through the CRI, and the CRI itself only
pulls -- so the push is done here, against the registry's own HTTP API.

Only what a push needs: ask what authentication is wanted, upload the
blobs that are not there yet, then the manifest. Blobs already in the
registry are not sent again, which for a commit is most of them -- the
image was pulled from there in the first place.
"""

import re

import requests

from oslo_log import log as logging

from zun.common import exception
from zun.common.i18n import _

LOG = logging.getLogger(__name__)

_CHUNK = 4 * 1024 * 1024


#: A challenge is comma-separated, and so is the value of its scope --
#: `scope="repository:x/y:pull,push"`. Splitting the whole line on commas
#: cuts that in half: the token is then asked for pull alone, and the
#: push it was wanted for is refused as 403, which reads like a
#: credential without permission rather than a request that asked for
#: less than it needed.
_FIELD = re.compile(r'(\w+)="([^"]*)"')


def _challenge_fields(challenge):
    """The key="value" pairs of an auth challenge, commas and all."""
    return dict(_FIELD.findall(challenge))


class Registry(object):
    """One registry, for one push."""

    def __init__(self, host, repository, username=None, password=None,
                 verify=True, timeout=120):
        self.base = '%s/v2' % host.rstrip('/')
        self.repository = repository
        self.auth = (username, password) if username else None
        self.verify = verify
        self.timeout = timeout
        self.session = requests.Session()
        self._token = None

    # ---------------------------------------------------------------- auth

    def _headers(self, extra=None):
        headers = dict(extra or {})
        if self._token:
            headers['Authorization'] = 'Bearer %s' % self._token
        return headers

    def _authenticate(self, response):
        """Get the token the registry just said it wanted.

        Returns whether anything changed, so a caller knows if retrying
        the request could go differently -- retrying with the same
        credentials that were just refused only makes two failures.
        """
        challenge = response.headers.get('WWW-Authenticate', '')
        if not challenge.lower().startswith('bearer '):
            return False
        fields = _challenge_fields(challenge[len('bearer '):])
        realm = fields.pop('realm', None)
        if not realm:
            return False
        fields.setdefault('scope',
                          'repository:%s:pull,push' % self.repository)
        answer = self.session.get(realm, params=fields, auth=self.auth,
                                  verify=self.verify, timeout=self.timeout)
        if answer.status_code != 200:
            raise exception.ZunException(_(
                'the registry refused these credentials (%s)')
                % answer.status_code)
        body = answer.json()
        token = body.get('token') or body.get('access_token')
        if not token:
            return False
        # Drop whatever the token endpoint set on the way. Harbor answers
        # it with a session cookie, and a request carrying both that
        # cookie and this token is authorised as the cookie -- anonymous
        # -- so a push that is perfectly entitled comes back 403.
        # Measured: same token, same session, 403 with the cookie and 202
        # without it.
        self.session.cookies.clear()
        self._token = token
        return True

    def _request(self, method, url, **kwargs):
        """One call, retried once if the registry asks for a token first."""
        kwargs.setdefault('verify', self.verify)
        kwargs.setdefault('timeout', self.timeout)
        headers = kwargs.pop('headers', None)
        response = self.session.request(method, url,
                                        headers=self._headers(headers),
                                        **kwargs)
        if response.status_code == 401 and self._authenticate(response):
            response = self.session.request(method, url,
                                            headers=self._headers(headers),
                                            **kwargs)
        return response

    # --------------------------------------------------------------- blobs

    def has_blob(self, digest):
        url = '%s/%s/blobs/%s' % (self.base, self.repository, digest)
        return self._request('HEAD', url).status_code == 200

    def mount_blob(self, digest, source_repository):
        """Ask the registry to reuse a blob it already has elsewhere.

        A commit adds one layer to an image that is already in this
        registry, so almost every blob is one it can mount rather than
        receive. Best effort: a registry that will not mount is told to
        take the bytes instead.
        """
        url = '%s/%s/blobs/uploads/' % (self.base, self.repository)
        response = self._request('POST', url, params={
            'mount': digest, 'from': source_repository})
        return response.status_code == 201

    def put_blob(self, digest, data):
        url = '%s/%s/blobs/uploads/' % (self.base, self.repository)
        started = self._request('POST', url)
        if started.status_code not in (202, 201):
            raise exception.ZunException(_(
                'the registry would not start an upload (%s)')
                % started.status_code)
        location = started.headers.get('Location')
        if not location:
            raise exception.ZunException(_(
                'the registry accepted an upload but said nowhere to put it'))
        if location.startswith('/'):
            location = self.base.rsplit('/v2', 1)[0] + location
        joiner = '&' if '?' in location else '?'
        finished = self._request(
            'PUT', '%s%sdigest=%s' % (location, joiner, digest), data=data,
            headers={'Content-Type': 'application/octet-stream'})
        if finished.status_code not in (201, 202):
            raise exception.ZunException(_(
                'the registry refused a blob (%(code)s): %(body)s')
                % {'code': finished.status_code,
                   'body': finished.text[:200]})

    def put_manifest(self, reference, media_type, data):
        url = '%s/%s/manifests/%s' % (self.base, self.repository, reference)
        response = self._request('PUT', url, data=data,
                                 headers={'Content-Type': media_type})
        if response.status_code not in (201, 202):
            raise exception.ZunException(_(
                'the registry refused the manifest (%(code)s): %(body)s')
                % {'code': response.status_code,
                   'body': response.text[:200]})
        return response.headers.get('Docker-Content-Digest')
