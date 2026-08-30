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

"""Reading the registry's authentication challenge.

A challenge separates its fields with commas, and so does the value of
its scope: `scope="repository:x/y:pull,push"`. Splitting the whole line
on commas cuts that in half, the token is asked for pull alone, and the
push it was wanted for comes back 403 -- which reads like a credential
without permission rather than a request that asked for less than it
needed. Measured against Harbor: with the scope whole, the same
credential is accepted.
"""

from unittest import mock

from zun.container.cri import registry as cri_registry
from zun.tests import base


_HARBOR = ('realm="https://harbor.tue.jp/service/token",'
           'service="harbor-registry",'
           'scope="repository:cri-commit-test/proof:pull,push"')


class ChallengeFieldsTest(base.TestCase):

    def test_the_scope_survives_its_own_comma(self):
        fields = cri_registry._challenge_fields(_HARBOR)

        self.assertEqual('repository:cri-commit-test/proof:pull,push',
                         fields['scope'])

    def test_the_other_fields_are_read_too(self):
        fields = cri_registry._challenge_fields(_HARBOR)

        self.assertEqual('https://harbor.tue.jp/service/token',
                         fields['realm'])
        self.assertEqual('harbor-registry', fields['service'])

    def test_nothing_spurious_is_invented(self):
        """The naive split produced a field called `push"`."""
        self.assertEqual({'realm', 'service', 'scope'},
                         set(cri_registry._challenge_fields(_HARBOR)))

    def test_a_challenge_without_a_scope_is_still_read(self):
        fields = cri_registry._challenge_fields(
            'realm="https://r/token",service="s"')

        self.assertEqual('https://r/token', fields['realm'])
        self.assertNotIn('scope', fields)


class TheTokenIsNotUndoneByACookieTest(base.TestCase):
    """Harbor answers its token endpoint with a session cookie.

    A request carrying both that cookie and the token is authorised as
    the cookie -- anonymous -- so a push that is perfectly entitled
    comes back 403. Measured: same token, same session, 403 with the
    cookie and 202 without it.
    """

    def _authenticated(self):
        registry = cri_registry.Registry('https://harbor.example', 'p/app',
                                         username='robot$p+r',
                                         password='secret')
        registry.session = mock.Mock()
        registry.session.get.return_value = mock.Mock(
            status_code=200, json=lambda: {'token': 't0ken'})
        challenge = mock.Mock(headers={
            'WWW-Authenticate': 'Bearer realm="https://harbor.example/token",'
                                'service="harbor-registry",'
                                'scope="repository:p/app:pull,push"'})
        return registry, registry._authenticate(challenge)

    def test_the_cookie_is_dropped_once_the_token_is_in_hand(self):
        registry, worked = self._authenticated()

        self.assertTrue(worked)
        registry.session.cookies.clear.assert_called_once_with()

    def test_the_whole_scope_is_asked_for(self):
        registry, _worked = self._authenticated()
        asked = registry.session.get.call_args.kwargs['params']

        self.assertEqual('repository:p/app:pull,push', asked['scope'])
