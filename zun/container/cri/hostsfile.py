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

"""The /etc/hosts a sandbox's containers are given.

⚠️ Left to itself the runtime copies the node's own file into every sandbox
(containerd internal/cri/server/podsandbox/sandbox_run_linux.go), which tells a
tenant the node's name and addresses, and whatever else the node's file lists.
It does not mount that copy over a mount the container already has for the path
(container_create.go), so each sandbox gets a file of its own, as a kubelet
gives each pod.
"""

import os
import re
import tempfile

from oslo_log import log as logging

LOG = logging.getLogger(__name__)

CONTAINER_PATH = '/etc/hosts'

_BASE = ('127.0.0.1\tlocalhost\n'
         '::1\tlocalhost ip6-localhost ip6-loopback\n'
         'fe00::0\tip6-localnet\n'
         'ff00::0\tip6-mcastprefix\n'
         'ff02::1\tip6-allnodes\n'
         'ff02::2\tip6-allrouters\n')

_NAME = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$')


def _path(root, uuid):
    return os.path.join(root, '%s.hosts' % uuid)


def own_addresses(addresses):
    """The addresses in a capsule's record, in the order they are listed."""
    found = []
    if not isinstance(addresses, dict):
        return found
    for entries in addresses.values():
        for entry in entries or []:
            addr = entry.get('addr') if isinstance(entry, dict) else None
            if addr and addr not in found:
                found.append(addr)
    return found


def render(hostname, addresses):
    """The file's text. A name that could not be a host name is left out."""
    lines = [_BASE]
    if isinstance(hostname, str) and _NAME.match(hostname):
        lines += ['%s\t%s\n' % (addr, hostname) for addr in addresses]
    return ''.join(lines)


def write(root, uuid, hostname, addresses):
    """Make the sandbox's file, and return the path to mount.

    Left alone if it already says this: a rebuild makes the containers again
    and the members still running hold the file by inode, so a rename would
    leave them with a different file from the ones made after it.
    """
    path = _path(root, uuid)
    content = render(hostname, addresses)
    try:
        with open(path) as fd:
            if fd.read() == content:
                return path
    except FileNotFoundError:
        pass
    os.makedirs(root, mode=0o755, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=root, prefix='.%s.' % uuid)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(content)
        os.chmod(temporary, 0o644)
        os.rename(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def remove(root, uuid):
    """Remove a sandbox's file, never failing the delete that called this."""
    try:
        os.unlink(_path(root, uuid))
    except FileNotFoundError:
        pass
    except OSError as e:
        LOG.warning('Could not remove the hosts file of %(uuid)s: %(err)s',
                    {'uuid': uuid, 'err': e})
