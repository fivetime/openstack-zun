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

"""A session's token, signed: checked before the upgrade, good once."""

import base64
import json
import os
import tempfile
import time
from unittest import mock
from urllib import parse as urlparse

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import jwt
from websockify import websockifyserver

from zun.common import exception
from zun.compute import api as compute_api
from zun.db import api as dbapi
from zun.objects.container import Container
from zun.tests import base
from zun.tests.unit.db import base as db_base
from zun.tests.unit.db import utils
from zun.websocket import console_token
from zun.websocket import exec_token
from zun.websocket import websocketproxy

UUID = 'ea8e2a25-2901-438d-8157-de7ffd68d051'


def _key_pair(directory, name):
    key = ec.generate_private_key(ec.SECP256R1())
    private = os.path.join(directory, name + '.key')
    public = os.path.join(directory, name + '.pub')
    with open(private, 'wb') as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(public, 'wb') as f:
        f.write(key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


class _Keys(object):

    def setUp(self):
        super(_Keys, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.unlink(os.path.join(self.dir, f))
                                 for f in os.listdir(self.dir)])
        self.private, self.public = _key_pair(self.dir, 'zun')
        self.config(token_signing_key=self.private,
                    token_verify_key=self.public, group='websocket_proxy')
        console_token._read.cache_clear()


class TestConsoleToken(_Keys, base.TestCase):

    def test_what_is_wrapped_is_what_is_unwrapped(self):
        token = console_token.wrap('attach', UUID, 'bare')

        self.assertEqual('bare', console_token.unwrap('attach', UUID, token))

    def test_it_is_a_jwt_anything_with_the_public_key_can_check(self):
        token = console_token.wrap('exec', UUID, 'bare', exec_id='e1')

        with open(self.public, 'rb') as f:
            claims = jwt.decode(token, f.read(), algorithms=['ES256'],
                                audience='zun-wsproxy', issuer='zun')
        self.assertEqual((UUID, 'exec', 'e1'),
                         (claims['sub'], claims['knd'], claims['eid']))
        self.assertEqual(60, claims['exp'] - claims['iat'])

    def test_a_stale_one_opens_nothing(self):
        self.config(console_token_ttl=5, group='websocket_proxy')
        # Handed out 11 s ago: past its 5, and the 5 s of leeway after.
        with mock.patch('time.time', return_value=time.time() - 11):
            token = console_token.wrap('attach', UUID, 'bare')

        self.assertRaises(exception.InvalidWebsocketToken,
                          console_token.unwrap, 'attach', UUID, token)

    def test_another_key_opens_nothing(self):
        private, _public = _key_pair(self.dir, 'other')
        self.config(token_signing_key=private, group='websocket_proxy')
        token = console_token.wrap('attach', UUID, 'bare')

        self.assertRaises(exception.InvalidWebsocketToken,
                          console_token.unwrap, 'attach', UUID, token)

    def test_it_opens_only_the_session_it_was_made_for(self):
        token = console_token.wrap('exec', UUID, 'bare', exec_id='e1')

        for kind, uuid, exec_id in (('attach', UUID, 'e1'),
                                    ('exec', 'other', 'e1'),
                                    ('exec', UUID, 'e2'),
                                    ('exec', UUID, None)):
            self.assertRaises(exception.InvalidWebsocketToken,
                              console_token.unwrap, kind, uuid, token,
                              exec_id)

    def test_an_altered_one_opens_nothing(self):
        token = console_token.wrap('attach', UUID, 'bare')
        head, body, sig = token.split('.')
        claims = json.loads(base64.urlsafe_b64decode(body + '=='))
        claims['exp'] += 3600
        body = base64.urlsafe_b64encode(
            json.dumps(claims).encode()).rstrip(b'=').decode()

        self.assertRaises(exception.InvalidWebsocketToken,
                          console_token.unwrap, 'attach', UUID,
                          '.'.join((head, body, sig)))

    def test_an_unsigned_one_opens_nothing(self):
        token = jwt.encode({'iss': 'zun', 'aud': 'zun-wsproxy', 'sub': UUID,
                            'knd': 'attach', 'tok': 'bare',
                            'iat': int(time.time()),
                            'exp': int(time.time()) + 60},
                           None, algorithm='none')

        self.assertRaises(exception.InvalidWebsocketToken,
                          console_token.unwrap, 'attach', UUID, token)

    def test_a_bare_token_is_not_accepted_once_checking(self):
        self.assertRaises(exception.InvalidWebsocketToken,
                          console_token.unwrap, 'attach', UUID, 'bare')

    def test_without_keys_a_token_is_the_bare_one(self):
        self.config(token_signing_key=None, token_verify_key=None,
                    group='websocket_proxy')

        self.assertEqual('bare', console_token.wrap('attach', UUID, 'bare'))
        self.assertEqual('bare',
                         console_token.unwrap('attach', UUID, 'bare'))


class TestTheApiSigns(_Keys, base.TestCase):

    def setUp(self):
        super(TestTheApiSigns, self).setUp()
        p = mock.patch('zun.scheduler.client.report.SchedulerReportClient')
        p.start()
        self.addCleanup(p.stop)
        self.config(exec_token_key='s3cret', group='websocket_proxy')
        self.container = Container(self.context,
                                   **utils.get_test_container(uuid=UUID))
        self.api = compute_api.API(self.context)
        # The node's answer, whatever the rpc layer's own checks would say.
        self.api.rpcapi = mock.Mock()
        self.rpc = self.api.rpcapi

    def _token(self, url):
        return urlparse.parse_qs(urlparse.urlparse(url).query)['token'][0]

    def test_attach(self):
        self.rpc.container_attach.return_value = {'token': 'bare',
                                                  'proxy_base': 'ws://p/'}

        url = self.api.container_attach(self.context, self.container)

        self.assertEqual('bare', console_token.unwrap(
            'attach', UUID, self._token(url)))

    def test_logs(self):
        self.rpc.container_logs_url.return_value = {'token': 'bare',
                                                    'proxy_base': 'ws://p/'}

        url = self.api.container_logs_url(self.context, self.container,
                                          True, True)

        self.assertEqual('bare', console_token.unwrap(
            'logs', UUID, self._token(url)))

    def test_interactive_exec(self):
        self.rpc.container_exec.return_value = {
            'token': 'bare', 'exec_id': 'e1', 'proxy_base': 'ws://p/',
            'output': None, 'exit_code': None}

        data = self.api.container_exec(self.context, self.container, 'sh',
                                       False, True)

        self.assertEqual('bare', console_token.unwrap(
            'exec', UUID, self._token(data['proxy_url']), 'e1'))

    def test_streamed_exec_lasts_its_own_ttl(self):
        self.config(exec_token_ttl=30, group='websocket_proxy')
        self.rpc.container_exec_stream.return_value = {
            'exec_id': 'e1', 'proxy_base': 'ws://p/',
            'target': {'kind': 'cri', 'url': 'http://x/', 'exec_id': None}}

        answer = self.api.container_exec_stream(self.context, self.container,
                                                'cat', False, None)

        token = self._token(answer['proxy_url'])
        claims = jwt.decode(token, options={'verify_signature': False})
        self.assertEqual(30, claims['exp'] - claims['iat'])
        sealed = console_token.unwrap('stream', UUID, token)
        self.assertEqual(UUID, exec_token.open_token(sealed)['uuid'])


class TestConsumed(db_base.DbTestCase):

    def test_an_attach_token_is_good_once(self):
        token = utils.get_test_container()['websocket_token']
        utils.create_test_container(context=self.context, uuid=UUID)

        self.assertTrue(dbapi.consume_console_token(
            self.context, UUID, 'attach', token))
        self.assertFalse(dbapi.consume_console_token(
            self.context, UUID, 'attach', token))

    def test_only_the_token_the_row_holds(self):
        utils.create_test_container(context=self.context, uuid=UUID,
                                    logs_token='t')

        self.assertFalse(dbapi.consume_console_token(
            self.context, UUID, 'logs', 'other'))
        self.assertFalse(dbapi.consume_console_token(
            self.context, UUID, 'logs', None))
        self.assertTrue(dbapi.consume_console_token(
            self.context, UUID, 'logs', 't'))

    def test_an_exec_token_is_good_once_for_its_exec(self):
        container = utils.create_test_container(context=self.context,
                                                uuid=UUID)
        utils.create_test_exec_instance(context=self.context,
                                        container_id=container.id,
                                        exec_id='e1', token='t')

        self.assertFalse(dbapi.consume_console_token(
            self.context, UUID, 'exec', 't', 'e2'))
        self.assertTrue(dbapi.consume_console_token(
            self.context, UUID, 'exec', 't', 'e1'))
        self.assertFalse(dbapi.consume_console_token(
            self.context, UUID, 'exec', 't', 'e1'))


class _Container(object):
    uuid = UUID
    websocket_token = 'bare'
    logs_token = 'bare-logs'
    exec_instances = []


class TestTheProxyChecksFirst(_Keys, base.TestCase):
    """Every kind of session is refused before the upgrade, not after."""

    def setUp(self):
        super(TestTheProxyChecksFirst, self).setUp()
        self.handler = mock.Mock(headers={'Host': 'proxy'})
        self.handler._find_container.return_value = _Container()
        p = mock.patch.object(dbapi, 'consume_console_token',
                              return_value=True)
        self.consume = p.start()
        self.addCleanup(p.stop)

    def _validate(self, token, query=''):
        self.handler.path = '/?token=%s&uuid=%s%s' % (token, UUID, query)
        return websocketproxy.ZunProxyRequestHandlerBase.validate_connection(
            self.handler)

    def _claim(self, *args):
        return websocketproxy.ZunProxyRequestHandlerBase._claim(
            self.handler, *args)

    def test_a_good_one_is_used_up_before_the_upgrade(self):
        self.handler._claim = self._claim

        self._validate(console_token.wrap('attach', UUID, 'bare'))

        self.consume.assert_called_once_with(mock.ANY, UUID, 'attach',
                                             'bare', None)
        container, token = self.handler.session
        self.assertEqual(('bare', 'bare'), (container.websocket_token, token))
        self.handler.send_response.assert_not_called()

    def test_one_used_before_is_a_403(self):
        self.handler._claim = self._claim
        self.consume.return_value = False

        self.assertRaises(websockifyserver.WebSockifyServer.EClose,
                          self._validate,
                          console_token.wrap('attach', UUID, 'bare'))

        self.handler.send_response.assert_called_once_with(403)

    def test_a_wrong_origin_does_not_use_the_token_up(self):
        self.handler._claim = self._claim
        self.handler._verify_origin.side_effect = exception.ValidationError(
            detail='Origin header does not match this host.')

        self.assertRaises(websockifyserver.WebSockifyServer.EClose,
                          self._validate,
                          console_token.wrap('attach', UUID, 'bare'))

        self.handler.send_response.assert_called_once_with(400)
        self.consume.assert_not_called()

    def test_a_forged_one_never_reaches_the_database(self):
        self.assertRaises(websockifyserver.WebSockifyServer.EClose,
                          self._validate, 'garbage', '&stream=logs')

        self.handler.send_response.assert_called_once_with(403)
        self.handler._claim.assert_not_called()
        self.consume.assert_not_called()

    def test_a_logs_token_does_not_open_the_attach_session(self):
        self.handler._claim = self._claim
        token = console_token.wrap('logs', UUID, 'bare-logs')

        self.assertRaises(websockifyserver.WebSockifyServer.EClose,
                          self._validate, token)
        self.consume.assert_not_called()

    def test_an_unknown_container_looks_like_a_bad_token(self):
        self.handler._claim = self._claim
        self.handler._find_container.side_effect = (
            exception.ContainerNotFound(container=UUID))

        self.assertRaises(websockifyserver.WebSockifyServer.EClose,
                          self._validate,
                          console_token.wrap('attach', UUID, 'bare'))

        self.handler.send_response.assert_called_once_with(403)

    def test_without_keys_nothing_changes_before_the_upgrade(self):
        self.config(token_verify_key=None, group='websocket_proxy')
        cls = websocketproxy.ZunProxyRequestHandler
        handler = cls.__new__(cls)
        handler.server = mock.Mock(token_plugin=None)
        handler.path = '/?token=bare&uuid=%s' % UUID
        handler._find_container = mock.Mock()

        handler.validate_connection()

        handler._find_container.assert_not_called()
        self.assertIsNone(handler.session)
