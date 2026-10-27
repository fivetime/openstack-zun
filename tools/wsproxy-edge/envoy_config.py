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

"""Render an Envoy config to stand in front of zun-wsproxy (JSON = YAML).

zun-wsproxy forks a process for every connection before it reads a byte of
it, which is fine behind something that turns the unauthenticated away and
not fine on an address anyone can reach. This is that something: it ends
TLS, checks the console token's signature and expiry (JWT, ES256, see
zun/websocket/console_token.py) with the public key alone, bounds how long
a connection may take to send its headers and how many there may be, and
only then hands the connection to zun-wsproxy listening on loopback.


Two filter chains on one listener, told apart by the connection's source:
  trusted  -- the gateway's (and nodes') addresses: a large connection pool
  others   -- everything else, e.g. a tenant that reaches the node directly:
              a small pool of its own and a shorter time to send headers
so that a flood of direct connections exhausts only the direct pool and
never the one the gateway's traffic uses.

Per-client concurrency is not something Envoy bounds; the pools above, a
per-client request rate, and short timeouts are what stands in for it. A
host firewall counting connections per source is the layer for that.

  python tools/wsproxy-edge/envoy_config.py > envoy.yaml
  python -m zun.websocket.console_token verify.pub > jwks.json

Environment (defaults in brackets): LISTEN [127.0.0.1], PORT [16800],
UPSTREAM_PORT [16791] (zun-wsproxy's wsproxy_port, on 127.0.0.1),
TRUSTED [127.0.0.1/32] (comma separated CIDRs of the gateway/nodes),
TLS_DIR [/etc/envoy] (tls.crt, tls.key), JWKS [/etc/envoy/jwks.json],
ADMIN_PORT [16901] (loopback), MAX_ALL [8192], MAX_TRUSTED [4096],
MAX_OTHERS [256], RATE_TRUSTED [200], RATE_OTHERS [10] (upgrade requests
per second per client address), MAX_SESSIONS [512] (sessions handed to
zun-wsproxy at once).
"""
import json
import os

E = os.environ.get
LISTEN = E('LISTEN', '127.0.0.1')
PORT = int(E('PORT', '16800'))
UPSTREAM_PORT = int(E('UPSTREAM_PORT', '16791'))
TRUSTED = [c for c in E('TRUSTED', '127.0.0.1/32').split(',') if c]
TLS_DIR = E('TLS_DIR', '/etc/envoy')
JWKS = E('JWKS', '/etc/envoy/jwks.json')


def cidr(c):
    addr, _, length = c.partition('/')
    return {'address_prefix': addr,
            'prefix_len': int(length or (128 if ':' in addr else 32))}


def chain(name, max_connections, headers_timeout, per_client_rate, match):
    hcm = {
        '@type': 'type.googleapis.com/envoy.extensions.filters.network.'
                 'http_connection_manager.v3.HttpConnectionManager',
        'stat_prefix': name,
        'codec_type': 'HTTP1',
        'use_remote_address': True,
        'request_headers_timeout': headers_timeout,
        # A session lasts as long as it is connected; a silent command is
        # not an idle one.
        'request_timeout': '0s',
        'stream_idle_timeout': '0s',
        'max_request_headers_kb': 16,
        'common_http_protocol_options': {
            'idle_timeout': '15s',
            'headers_with_underscores_action': 'REJECT_REQUEST'},
        'upgrade_configs': [{'upgrade_type': 'websocket'}],
        'route_config': {'name': name, 'virtual_hosts': [{
            'name': 'wsproxy', 'domains': ['*'],
            'routes': [
                {'match': {'prefix': '/', 'headers': [{
                    'name': 'upgrade',
                    'string_match': {'exact': 'websocket',
                                     'ignore_case': True}}]},
                 'route': {'cluster': 'wsproxy', 'timeout': '0s',
                           'idle_timeout': '0s'}},
                {'match': {'prefix': '/'},
                 'direct_response': {'status': 404}}]}]},
        'http_filters': [
            {'name': 'envoy.filters.http.local_ratelimit',
             'typed_config': {
                 '@type': 'type.googleapis.com/envoy.extensions.filters.'
                          'http.local_ratelimit.v3.LocalRateLimit',
                 'stat_prefix': name + '_per_client',
                 'token_bucket': {'max_tokens': 1000000,
                                  'tokens_per_fill': 1000000,
                                  'fill_interval': '1s'},
                 'filter_enabled': {
                     'runtime_key': name + '_rl_enabled',
                     'default_value': {'numerator': 100,
                                       'denominator': 'HUNDRED'}},
                 'filter_enforced': {
                     'runtime_key': name + '_rl_enforced',
                     'default_value': {'numerator': 100,
                                       'denominator': 'HUNDRED'}},
                 'rate_limits': [{'actions': [{'remote_address': {}}]}],
                 'descriptors': [{
                     'entries': [{'key': 'remote_address'}],
                     'token_bucket': {
                         'max_tokens': per_client_rate * 2,
                         'tokens_per_fill': per_client_rate,
                         'fill_interval': '1s'}}],
                 'max_dynamic_descriptors': 20000}},
            {'name': 'envoy.filters.http.jwt_authn',
             'typed_config': {
                 '@type': 'type.googleapis.com/envoy.extensions.filters.'
                          'http.jwt_authn.v3.JwtAuthentication',
                 'providers': {'zun': {
                     'issuer': 'zun',
                     'audiences': ['zun-wsproxy'],
                     'local_jwks': {'filename': JWKS},
                     'from_params': ['token'],
                     'forward': True,
                     'require_expiration': True,
                     'max_lifetime': '600s',
                     'clock_skew_seconds': 5}},
                 'rules': [{'match': {'prefix': '/'},
                            'requires': {'provider_name': 'zun'}}]}},
            {'name': 'envoy.filters.http.router',
             'typed_config': {
                 '@type': 'type.googleapis.com/envoy.extensions.filters.'
                          'http.router.v3.Router'}}]}
    c = {
        'name': name,
        'transport_socket_connect_timeout': headers_timeout,
        'transport_socket': {
            'name': 'envoy.transport_sockets.tls',
            'typed_config': {
                '@type': 'type.googleapis.com/envoy.extensions.'
                         'transport_sockets.tls.v3.DownstreamTlsContext',
                'common_tls_context': {
                    'tls_params': {'tls_minimum_protocol_version': 'TLSv1_2'},
                    'tls_certificates': [{
                        'certificate_chain': {'filename':
                                              TLS_DIR + '/tls.crt'},
                        'private_key': {'filename':
                                        TLS_DIR + '/tls.key'}}]}}},
        'filters': [
            {'name': 'envoy.filters.network.connection_limit',
             'typed_config': {
                 '@type': 'type.googleapis.com/envoy.extensions.filters.'
                          'network.connection_limit.v3.ConnectionLimit',
                 'stat_prefix': name,
                 'max_connections': max_connections}},
            {'name': 'envoy.filters.network.http_connection_manager',
             'typed_config': hcm}],
    }
    if match:
        c['filter_chain_match'] = match
    return c


config = {
    'admin': {'address': {'socket_address': {
        'address': '127.0.0.1', 'port_value': int(E('ADMIN_PORT', '16901'))}}},
    'overload_manager': {'resource_monitors': [{
        'name': 'envoy.resource_monitors.global_downstream_max_connections',
        'typed_config': {
            '@type': 'type.googleapis.com/envoy.extensions.resource_monitors.'
                     'downstream_connections.v3.DownstreamConnectionsConfig',
            'max_active_downstream_connections': int(E('MAX_ALL', '8192'))}}]},
    'static_resources': {
        'listeners': [{
            'name': 'zun_wsproxy',
            'address': {'socket_address': {'address': LISTEN,
                                           'port_value': PORT}},
            'per_connection_buffer_limit_bytes': 65536,
            'filter_chains': [chain(
                'trusted', int(E('MAX_TRUSTED', '4096')), '10s',
                int(E('RATE_TRUSTED', '200')),
                {'source_prefix_ranges': [cidr(c) for c in TRUSTED]})],
            'default_filter_chain': chain(
                'others', int(E('MAX_OTHERS', '256')), '3s',
                int(E('RATE_OTHERS', '10')), None),
        }],
        'clusters': [{
            'name': 'wsproxy',
            'type': 'STATIC',
            'connect_timeout': '2s',
            'circuit_breakers': {'thresholds': [{
                'max_connections': int(E('MAX_SESSIONS', '512')),
                'max_requests': int(E('MAX_SESSIONS', '512')),
                'max_pending_requests': 64}]},
            'load_assignment': {'cluster_name': 'wsproxy', 'endpoints': [{
                'lb_endpoints': [{'endpoint': {'address': {
                    'socket_address': {'address': '127.0.0.1',
                                       'port_value': UPSTREAM_PORT}}}}]}]}}],
    },
}
print(json.dumps(config, indent=1))
