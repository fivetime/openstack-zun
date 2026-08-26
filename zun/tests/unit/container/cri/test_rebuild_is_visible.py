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

"""Starting a container the runtime will not restart.

The CRI does not start an exited container again, so the driver rebuilds
it from its image in the sandbox it died in. The address and the volumes
survive; anything written inside the container does not. The start
succeeds either way, which is why it has to say so -- a caller who wrote
something into the old one and got a successful start would otherwise
have no way to learn it is gone.
"""

from unittest import mock

import grpc

from zun.common import consts
from zun.container.cri import driver as cri_driver
from zun.tests import base


class _Exited(grpc.RpcError):
    def details(self):
        return 'failed to start: CONTAINER_EXITED'

    def code(self):
        return grpc.StatusCode.UNKNOWN


class RebuildIsVisibleTest(base.TestCase):

    def setUp(self):
        super(RebuildIsVisibleTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        self.container = mock.Mock(uuid='u-1', container_id='old-id',
                                   status_reason='an older failure')

    def _start(self, exited):
        if exited:
            self.driver.runtime_stub.StartContainer.side_effect = _Exited()
        with mock.patch.object(self.driver, '_restart_exited') as rebuilt:
            def rebuild(context, container):
                container.container_id = 'new-id'
                container.status_reason = self.driver.REBUILT_REASON
            rebuilt.side_effect = rebuild
            return self.driver.start({}, self.container), rebuilt

    def test_a_plain_start_leaves_no_reason_behind(self):
        """A reason from an earlier failure must not outlive it."""
        container, rebuilt = self._start(exited=False)

        rebuilt.assert_not_called()
        self.assertIsNone(container.status_reason)
        self.assertEqual(consts.RUNNING, container.status)

    def test_a_rebuild_says_so_in_the_reason(self):
        container, rebuilt = self._start(exited=True)

        rebuilt.assert_called_once()
        self.assertEqual(self.driver.REBUILT_REASON, container.status_reason)
        self.assertEqual(consts.RUNNING, container.status)

    def test_the_reason_says_what_was_kept_and_what_was_not(self):
        reason = str(cri_driver.CriDriver.REBUILT_REASON)

        self.assertIn('rebuilt', reason)
        self.assertIn('volumes are unchanged', reason)
        self.assertIn('written inside the container itself is not', reason)
