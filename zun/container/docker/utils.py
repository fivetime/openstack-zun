# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import contextlib
import socket
import struct
import sys
import tarfile
import time

import docker
from docker import errors
from oslo_serialization import jsonutils
from oslo_utils import encodeutils

from zun.common import consts
from zun.common import exception
from zun.common.i18n import _
from zun.common import utils
import zun.conf


CONF = zun.conf.CONF


def read_exec_output(sock, timeout):
    """All a non-tty exec writes, or socket.timeout after `timeout` seconds.

    `sock` is what exec_start(socket=True) hands back. The result is what
    exec_start would have returned without it: the payload of every frame
    of the multiplexed stream, stdout and stderr in the order written.

    docker-py cannot give up on its own: before each recv it polls the
    socket with no timeout at all, so a command that keeps quiet holds
    the caller for as long as the command lives. Under eventlet a
    Timeout used to interrupt that; on native threads nothing can, so
    the deadline is kept here, as a timeout on every recv of whatever
    time is left.
    """
    # SocketIO over a unix or plain TCP socket; the socket itself for TLS
    raw = getattr(sock, '_sock', sock)
    deadline = time.monotonic() + timeout
    received = []
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('exec did not finish in %ss' % timeout)
            raw.settimeout(remaining)
            data = raw.recv(65536)
            if not data:
                break
            received.append(data)
    finally:
        # Hang up for good: the response object still holds the socket,
        # and a plain close() leaves it open while anything does.
        try:
            raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        raw.close()

    stream = b''.join(received)
    payload = []
    offset = 0
    while offset + 8 <= len(stream):
        # Frame header: stream id, three bytes of padding, payload length
        _stream_id, length = struct.unpack_from('>BxxxL', stream, offset)
        offset += 8
        payload.append(stream[offset:offset + length])
        offset += length
    return b''.join(payload)


@contextlib.contextmanager
def docker_client():
    client_kwargs = dict()
    if not CONF.docker.api_insecure:
        client_kwargs['ca_cert'] = CONF.docker.ca_file
        client_kwargs['client_key'] = CONF.docker.key_file
        client_kwargs['client_cert'] = CONF.docker.cert_file

    try:
        yield DockerHTTPClient(
            CONF.docker.api_url,
            CONF.docker.docker_remote_api_version,
            CONF.docker.default_timeout,
            **client_kwargs
        )
    except errors.APIError as e:
        desired_exc = exception.DockerError(error_msg=str(e))
        utils.reraise(type(desired_exc), desired_exc, sys.exc_info()[2])


class DockerHTTPClient(docker.APIClient):
    def __init__(self, url=CONF.docker.api_url,
                 ver=CONF.docker.docker_remote_api_version,
                 timeout=CONF.docker.default_timeout,
                 ca_cert=None,
                 client_key=None,
                 client_cert=None):

        if ca_cert and client_key and client_cert:
            ssl_config = docker.tls.TLSConfig(
                client_cert=(client_cert, client_key),
                verify=ca_cert
            )
        else:
            ssl_config = False

        super(DockerHTTPClient, self).__init__(
            base_url=url,
            version=ver,
            timeout=timeout,
            tls=ssl_config
        )

    def list_containers(self):
        return self.containers(all=True, filters={'name': consts.NAME_PREFIX})

    def read_tar_image(self, image):
        image_path = image['path']
        with tarfile.open(image_path, 'r') as fil:
            fest = fil.extractfile('manifest.json')
            data = fest.read()
            data = jsonutils.loads(encodeutils.safe_decode(data))
            repo_tags = data[0]['RepoTags']
            if repo_tags:
                repo, tag = repo_tags[0].split(":")
                image['repo'], image['tag'] = repo, tag
            else:
                image_uuid = data[0]['Config'].split('.')[0]
                image['repo'], image['tag'] = image_uuid, ''

    def exec_resize(self, exec_id, height=None, width=None):
        # NOTE(hongbin): This is a temporary work-around for a docker issue
        # See: https://github.com/moby/moby/issues/35561
        try:
            super(DockerHTTPClient, self).exec_resize(
                exec_id, height=height, width=width)
        except errors.APIError as e:
            if "process not found for container" in str(e):
                raise exception.Invalid(_(
                    "no such exec instance: %s") % str(e))
            raise
