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

"""What a caller is told when a compute node stops answering."""

from unittest import mock

import oslo_messaging

from zun.common import consts
from zun.common import exception
from zun.common import utils
from zun.tests import base


class TestComputeNodeUnresponsive(base.TestCase):
    """A timed-out RPC is not the same as a server error.

    The work may still be running: the request was neither accepted nor
    refused. A caller that retries can end up with two of whatever it
    asked for, and one that gives up may find it there later. Both
    mistakes were on offer, because every non-Zun exception -- this one
    included -- was answered with the same obfuscated 500 and a
    correlation id.
    """

    def _wrapped(self, raises):
        def controller(self_):
            raise raises

        server_error = mock.Mock(side_effect=AssertionError('server error'))
        client_error = mock.Mock(
            side_effect=lambda message, code: (message, code))
        return exception.wrap_controller_exception(
            controller, server_error, client_error)

    def test_a_timeout_is_answered_as_a_gateway_timeout(self):
        wrapped = self._wrapped(oslo_messaging.MessagingTimeout('no reply'))

        message, code = wrapped(mock.Mock())

        self.assertEqual(504, code)

    def test_the_reason_survives_instead_of_being_obfuscated(self):
        wrapped = self._wrapped(oslo_messaging.MessagingTimeout('no reply'))

        message, _code = wrapped(mock.Mock())

        self.assertIn('did not answer', str(message))

    def test_it_says_the_request_was_neither_taken_nor_refused(self):
        """Which is the one thing the caller can act on."""
        wrapped = self._wrapped(oslo_messaging.MessagingTimeout('no reply'))

        message, _code = wrapped(mock.Mock())

        self.assertIn('neither accepted nor refused', str(message))

    def test_the_exception_carries_the_code_by_itself(self):
        self.assertEqual(504, exception.ComputeNodeUnresponsive.code)


class TestRemovingAContainerStuckCreating(base.TestCase):
    """A create whose reply never came leaves a container nobody can drop.

    It has no host -- the endpoint calls a compute node only when there
    is one -- so the delete is a record to drop and nothing else. But
    CREATING was the one state left out of delete_after_stop, and the
    wider delete_force that does cover it is admin-only. The owner was
    told no by both, and the name stayed taken for good.
    """

    def test_the_owner_can_remove_it(self):
        self.assertIn(consts.CREATING,
                      utils.VALID_STATES['delete_after_stop'])

    def test_the_states_that_were_already_allowed_still_are(self):
        for state in (consts.RUNNING, consts.CREATED, consts.ERROR,
                      consts.STOPPED, consts.DELETED, consts.DEAD):
            self.assertIn(state, utils.VALID_STATES['delete_after_stop'])

    def test_it_passes_validation_now(self):
        container = mock.Mock(uuid='u-1', status=consts.CREATING)

        utils.validate_container_state(container, 'delete_after_stop')

    def test_a_state_that_was_never_allowed_is_still_refused(self):
        container = mock.Mock(uuid='u-1', status=consts.PAUSED)

        self.assertRaises(exception.InvalidStateException,
                          utils.validate_container_state,
                          container, 'delete_after_stop')


class TestTheReportingTickIsFineEnough(base.TestCase):
    """The configured interval was silently doubled.

    `_due` can only answer on a tick, so a tick coarser than the
    shortest configurable interval rounds every finer setting up to
    itself. With a thirty second tick, `report_interval = 15` reported
    every thirty -- measured on a live stream, where two consecutive
    readings were exactly thirty seconds apart.

    Reading the interval at runtime rather than in the decorator fixes
    the half where the default froze. This is the other half.
    """

    def test_the_tick_is_no_coarser_than_the_shortest_interval(self):
        from zun.compute import manager
        import zun.conf

        shortest = [opt.type.min for opt in zun.conf.usage.usage_opts
                    if opt.name == 'report_interval'][0]

        self.assertLessEqual(manager._REPORT_TICK, shortest)
