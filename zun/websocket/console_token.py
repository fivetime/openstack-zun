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

"""The token in a session's url, signed so it can be checked anywhere.

A session -- attach, interactive exec, logs, streamed exec -- is reached
with a token zun-api hands out. Bare, that token is a row's value: only
something that reads the database can tell a good one from a guess, so a
proxy has to take every connection in before it can turn any away, and a
token handed out once opens the session for as long as the row keeps it.

Signed, it is a JWT (ES256) carrying the bare token, which session it is
for, and when it stops being any good. Whatever stands in front of the
proxy (Envoy's jwt_authn, say) refuses a forgery or a stale one with the
public key alone, and so does the proxy, before it upgrades the connection.
The bare token inside still has to match the row, and the proxy clears the
row's on use: one token, one connection.

Signing is on when zun-api has a key; checking, when zun-wsproxy has the
public half. Without them a token is the bare one, as it always was.
"""

import functools
import time

import jwt
from oslo_utils import uuidutils

from zun.common import exception
import zun.conf

CONF = zun.conf.CONF

ALGORITHM = 'ES256'
ISSUER = 'zun'
AUDIENCE = 'zun-wsproxy'

ATTACH = 'attach'
EXEC = 'exec'
LOGS = 'logs'
STREAM = 'stream'

# A clock a little ahead of another's should not turn a fresh token away.
LEEWAY = 5


@functools.lru_cache(maxsize=4)
def _read(path):
    with open(path, 'rb') as f:
        return f.read()


def signing():
    return bool(CONF.websocket_proxy.token_signing_key)


def checking():
    return bool(CONF.websocket_proxy.token_verify_key)


def wrap(kind, uuid, token, exec_id=None, ttl=None):
    """What goes in the url for session `kind` of container `uuid`."""
    if not signing() or token is None:
        return token
    now = int(time.time())
    claims = {
        'iss': ISSUER,
        'aud': AUDIENCE,
        'sub': uuid,
        'knd': kind,
        'tok': token,
        'iat': now,
        'exp': now + (ttl or CONF.websocket_proxy.console_token_ttl),
        'jti': uuidutils.generate_uuid(),
    }
    if exec_id:
        claims['eid'] = exec_id
    return jwt.encode(claims,
                      _read(CONF.websocket_proxy.token_signing_key),
                      algorithm=ALGORITHM)


def unwrap(kind, uuid, token, exec_id=None):
    """The bare token in `token`, or InvalidWebsocketToken.

    Bad signature, expired, for another container, another kind of session
    or another exec: all the same to the caller, who learns only that this
    token does not open anything here.
    """
    if not checking():
        return token
    try:
        claims = jwt.decode(
            token, _read(CONF.websocket_proxy.token_verify_key),
            algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER,
            leeway=LEEWAY,
            options={'require': ['exp', 'iat', 'sub', 'knd', 'tok']})
    except (jwt.PyJWTError, ValueError, TypeError):
        raise exception.InvalidWebsocketToken()
    if (claims['sub'] != uuid or claims['knd'] != kind or
            claims.get('eid') != (exec_id or None)):
        raise exception.InvalidWebsocketToken()
    return claims['tok']


def jwks(public_pem):
    """`public_pem` as a JWKS, which is what Envoy's jwt_authn reads."""
    key = jwt.algorithms.ECAlgorithm.to_jwk(
        jwt.algorithms.ECAlgorithm(jwt.algorithms.ECAlgorithm.SHA256)
        .prepare_key(public_pem), as_dict=True)
    key.update({'alg': ALGORITHM, 'use': 'sig'})
    return {'keys': [key]}


if __name__ == '__main__':
    # python -m zun.websocket.console_token <public key PEM> > jwks.json
    import json
    import sys

    with open(sys.argv[1], 'rb') as f:
        print(json.dumps(jwks(f.read())))
