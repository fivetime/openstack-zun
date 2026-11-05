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

"""What a tenant may read about a failure.

A runtime's error names this node's resolver, paths and internal API, and on
kata a whole backtrace. Measured on a production create: the image-pull error
a tenant was shown named the node's DNS address. Whatever is matched below only
chooses among sentences written here, so nothing of the error itself is passed
on; the log keeps it.
"""

import re

from zun.common import exception
from zun.common.i18n import _

NOT_CREATED = _('The container could not be created')
NOT_STARTED = _('The container could not be started')
NOT_DELETED = _('The container could not be deleted')
NOT_REBUILT = _('The container could not be rebuilt')
NO_ROOM = _('The node chosen for this container did not have enough room')
IMAGE_NOT_PULLED = _('The image could not be pulled')
IMAGE_NOT_FOUND = _('The image could not be pulled: it was not found')
IMAGE_DENIED = _('The image could not be pulled: access was denied')
REGISTRY_UNREACHABLE = _('The image could not be pulled: its registry could '
                         'not be reached')
COMMAND_NOT_FOUND = _("The container's command was not found in its image")
COMMAND_DENIED = _("The container's command could not be run: permission "
                   "denied")

# Written to be read by the owner of the container. A bare ZunException is
# not: it is what this code raises for its operators, with paths in it.
_WRITTEN_FOR_THE_TENANT = (exception.VolumeNotPrepared,
                           exception.InitContainerFailed,
                           exception.CapsuleCreateAbandoned)

_PULL = re.compile(r'failed to pull|error on pulling image|pull access denied'
                   r'|manifest unknown', re.I)
_DENIED = re.compile(r'denied|unauthori[sz]ed|forbidden', re.I)
_UNREACHABLE = re.compile(r'no such host|connection refused|timeout'
                          r'|network is unreachable', re.I)
_NOT_FOUND = re.compile(r'not found|manifest unknown', re.I)
_EXEC = r'exec: "[^"]*":.*'
_COMMAND_MISSING = re.compile(
    _EXEC + r'(no such file|executable file not found)'
    r'|the file \S+ was not found|executable file not found', re.I)
_COMMAND_DENIED = re.compile(_EXEC + r'permission denied', re.I)


def _of_a_pull(text):
    if _DENIED.search(text):
        return IMAGE_DENIED
    if _UNREACHABLE.search(text):
        return REGISTRY_UNREACHABLE
    if _NOT_FOUND.search(text):
        return IMAGE_NOT_FOUND
    return IMAGE_NOT_PULLED


def for_tenant(error, fallback):
    """The reason to record for this error, or the fallback when none fits."""
    if isinstance(error, _WRITTEN_FOR_THE_TENANT):
        return str(error)
    if isinstance(error, exception.ResourcesUnavailable):
        return NO_ROOM
    if isinstance(error, exception.ImageNotFound):
        return IMAGE_NOT_FOUND
    text = str(error)
    if _PULL.search(text):
        return _of_a_pull(text)
    if _COMMAND_MISSING.search(text):
        return COMMAND_NOT_FOUND
    if _COMMAND_DENIED.search(text):
        return COMMAND_DENIED
    # Refusals answered to the API as well, about what was asked for.
    if isinstance(error, exception.Invalid):
        return text
    return fallback
