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

"""The token a streamed exec (API 1.59) is reached by.

Everything the proxy needs travels inside it -- which container, where the
runtime serves the command, whether stdin is open, how long it may run --
sealed by zun-api and opened by zun-wsproxy with a secret both read. Nothing
is written anywhere: there is no row to clean up after a caller who never
connects. Sealed rather than only signed, because what is inside includes the
runtime's own address on a compute node, which is nobody's business.

Single use is the runtime's doing, not this one's: docker starts an exec only
once, and the runtime's streaming url is consumed by the first connection.
"""

import base64
import hashlib

from cryptography import fernet
from oslo_serialization import jsonutils

from zun.common import exception
from zun.common.i18n import _
import zun.conf

CONF = zun.conf.CONF


def _fernet():
    secret = CONF.websocket_proxy.exec_token_key
    if not secret:
        raise exception.Conflict(_(
            'streamed exec is not enabled on this cloud: '
            '[websocket_proxy] exec_token_key is not set'))
    key = hashlib.sha256(secret.encode('utf-8')).digest()
    return fernet.Fernet(base64.urlsafe_b64encode(key))


def ensure_enabled():
    """Refuse a streamed exec where its token cannot be made."""
    _fernet()


def seal(payload):
    """A token carrying `payload`, a JSON-able dict."""
    return _fernet().encrypt(
        jsonutils.dump_as_bytes(payload)).decode('ascii')


def open_token(token):
    """The payload sealed in `token`, or InvalidWebsocketToken.

    Expired, altered and made with another secret all look the same from
    here, and are told apart to nobody: a caller learns only that this
    token does not open anything.
    """
    try:
        data = _fernet().decrypt(
            token.encode('ascii'), ttl=CONF.websocket_proxy.exec_token_ttl)
    except (fernet.InvalidToken, UnicodeEncodeError, AttributeError):
        raise exception.InvalidWebsocketToken()
    return jsonutils.loads(data)
