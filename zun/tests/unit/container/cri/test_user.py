#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
#    implied. See the License for the specific language governing
#    permissions and limitations under the License.

"""Which user a container's process runs as.

docker writes one string in four shapes -- `uid`, `uid:gid`, `name` and
`name:group` -- and the runtime interface splits the same thing across
three fields. A container asking to drop to an unprivileged user and
running as root instead has been answered with the opposite of what it
asked, about its own privilege, and nothing says so.
"""

from unittest import mock

from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.criapi import api_pb2
from zun.tests import base


def _context(user):
    """The security context this driver would send for `user`."""
    kwargs = {}
    cri_driver._apply_user(mock.Mock(user=user), kwargs)
    return kwargs


class ApplyUserTest(base.TestCase):

    def test_a_uid_is_a_number(self):
        self.assertEqual({'run_as_user': api_pb2.Int64Value(value=1000)},
                         _context('1000'))

    def test_a_uid_and_gid_are_both_numbers(self):
        self.assertEqual({'run_as_user': api_pb2.Int64Value(value=1000),
                          'run_as_group': api_pb2.Int64Value(value=2000)},
                         _context('1000:2000'))

    def test_a_name_is_carried_for_the_image_to_resolve(self):
        """Only the image knows what `nobody` is."""
        self.assertEqual({'run_as_username': 'nobody'}, _context('nobody'))

    def test_a_name_with_a_numeric_group(self):
        self.assertEqual({'run_as_username': 'nobody',
                          'run_as_group': api_pb2.Int64Value(value=65534)},
                         _context('nobody:65534'))

    def test_root_is_still_a_request(self):
        """uid 0 is asked for explicitly, not the same as asking nothing."""
        self.assertEqual({'run_as_user': api_pb2.Int64Value(value=0)},
                         _context('0'))

    def test_nothing_asked_sets_nothing(self):
        """An unset field leaves the image's own USER in charge."""
        self.assertEqual({}, _context(None))
        self.assertEqual({}, _context(''))

    def test_a_named_group_is_refused_rather_than_dropped(self):
        """There is no run_as_groupname to put it in.

        Dropping it would give the container a group it did not ask for,
        and files nobody else in the intended group can read.
        """
        error = self.assertRaises(exception.Invalid,
                                  _context, 'nobody:nogroup')

        self.assertIn('by number', str(error))
        self.assertIn('nobody:nogroup', str(error))


class SecurityContextTest(base.TestCase):
    """The field reaches the context the sandbox is built with."""

    def test_the_user_reaches_the_security_context(self):
        container = mock.Mock(user='1000:2000', privileged=False,
                              healthcheck=None)

        context = cri_driver._linux_security_context(container)

        self.assertEqual(1000, context.run_as_user.value)
        self.assertEqual(2000, context.run_as_group.value)

    def test_a_capsules_security_context_still_wins(self):
        """It is the more specific of the two requests."""
        container = mock.Mock(
            user='1000', privileged=False,
            healthcheck={'k8s_security_context': {'runAsUser': 4242}})

        context = cri_driver._linux_security_context(container)

        self.assertEqual(4242, context.run_as_user.value)
