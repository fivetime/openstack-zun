#    Copyright 2017 Linaro Limited
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

from oslo_config import cfg

wsproxy_group = cfg.OptGroup("websocket_proxy",
                             title="Websocket Proxy Group",
                             help="""
Users use the websocket proxy to connect to containers, instead of
connecting to containers directly, hence protects the socket daemon.
""")

wsproxy_opts = [
    cfg.URIOpt('base_url',
               default='ws://$wsproxy_host:$wsproxy_port/',
               help="""
The URL an end user would use to connect to the ``zun-wsproxy`` service.

The ``zun-wsproxy`` service is called with this token enriched URL
and establishes the connection to the proper instance.

Related options:

* The IP address must be the same as the address to which the
  ``zun-wsproxy`` service is listening (see option ``wsproxy_host``
  in this section).
* The port must be the same as ``wsproxy_port``in this section.
    """),
    cfg.StrOpt('wsproxy_host',
               default='$my_ip',
               help="""
The IP address which is used by the ``zun-wsproxy`` service to listen
for incoming requests.

The ``zun-wsproxy`` service listens on this IP address for incoming
connection requests.

Related options:

* Ensure that this is the same IP address which is defined in the option
  ``base_url`` of this section or use ``0.0.0.0`` to listen on all addresses.
"""),
    cfg.PortOpt('wsproxy_port',
                default=6784,
                help="""
The port number which is used by the ``zun-wsproxy`` service to listen
for incoming requests.

The ``zun-wsproxy`` service listens on this port number for incoming
connection requests.

Related options:

* Ensure that this is the same port number as that defined in the option
  ``base_url`` of this section.
"""),
    cfg.ListOpt('allowed_origins',
                default=[],
                help="""
Adds list of allowed origins to the console websocket proxy to allow
connections from other origin hostnames.
Websocket proxy matches the host header with the origin header to
prevent cross-site requests. This list specifies if any there are
values other than host are allowed in the origin header.

Possible values:

* A list where each element is an allowed origin hostnames, else an empty list
"""),
    cfg.BoolOpt('ssl_only',
                help="Disallow non-encrypted connections."),
    cfg.StrOpt('cert',
               default='',
               help="Path to SSL certificate file."),
    cfg.StrOpt('key',
               default='',
               help="SSL key file (if separate from cert)."),
    cfg.StrOpt('exec_token_key',
               secret=True,
               help="""
The secret a streamed exec's token is sealed with (API 1.59).

zun-api seals what the proxy needs to reach the command into the token it
hands out, and zun-wsproxy opens it; nothing is stored. Both services read
this option, so it has to be the same on each of them. Any string; it is
hashed into the key. Streamed exec is refused while it is unset.
"""),
    cfg.IntOpt('exec_token_ttl',
               default=30,
               min=1,
               help="""
Seconds a streamed exec's token may be used for once handed out. The
command does not start until the token is used, so this bounds only how
long a caller may take to connect.
"""),
    cfg.StrOpt('token_signing_key',
               help="""
Path to the PEM private key (EC P-256) zun-api signs a console token with.

When set, every url zun-api answers for a session -- attach, interactive
exec, logs and streamed exec -- carries a JWT (ES256) instead of the bare
token: signed, valid for ``console_token_ttl`` seconds, and good for one
connection. Whatever stands in front of zun-wsproxy can check it with the
public key alone, before a connection costs the proxy anything. Unset, the
token is the bare one, and never expires.

Related options:

* ``token_verify_key``, the public half, read by zun-wsproxy.
"""),
    cfg.StrOpt('token_verify_key',
               help="""
Path to the PEM public key zun-wsproxy checks a console token with.

When set, zun-wsproxy accepts a session only with a token signed by the
matching ``token_signing_key``, unexpired, and not used before; anything
else is refused before the websocket is upgraded. Set it together with
``token_signing_key`` on zun-api.
"""),
    cfg.IntOpt('console_token_ttl',
               default=60,
               min=5,
               help="""
Seconds a signed attach, interactive exec or logs token may be used for once
handed out. It bounds only how long a caller may take to connect; a session
lasts as long as it is connected. Reconnecting takes a new one from the API.
A streamed exec's token lasts ``exec_token_ttl`` instead.
"""),
    cfg.IntOpt('heartbeat',
               default=0,
               min=0,
               help="""
Seconds after which zun-wsproxy pings a client it has sent nothing to, on
every kind of session; 0 never pings.

Set it below the idle timeout of anything between the proxy and its
clients -- a load balancer or a gateway that cuts a stream nothing has
crossed for a while (Envoy's default is 300 seconds) would otherwise end a
session whose command is merely quiet.
"""),
    cfg.IntOpt('stream_buffer_limit',
               default=1048576,
               min=65536,
               help="""
Bytes of a streamed exec's output held for a client that reads slower than
the command writes. Past it the proxy stops reading from the container, and
the command blocks on its own output until the client catches up.
"""),
]

ALL_OPTS = (wsproxy_opts)


def register_opts(conf):
    conf.register_group(wsproxy_group)
    conf.register_opts(wsproxy_opts, group=wsproxy_group)


def list_opts():
    return {wsproxy_group: ALL_OPTS}
