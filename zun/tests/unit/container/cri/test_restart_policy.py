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

"""A container's restart policy, kept by this driver because the CRI has none.

Kubernetes keeps it in the kubelet, which watches container state and
recreates. Nothing here was doing that: `--restart always` reached the
record and nothing read it, so a container that died on a CRI host stayed
dead while the same request on a docker host came back.

"It died" and "it was stopped" look the same to the runtime: EXITED, with
a code. The difference is recorded where the stop happens -- stop() marks
the record -- so a stopped container carrying an exit code is one that
died. The sweep reads that mark rather than relying on having watched the
transition itself: a show usually sees the exit first and writes STOPPED
before the sweep loads the record. The decision is confirmed against a
fresh read under the lock the stop path takes.
"""

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


class RestartOnExitTest(base.TestCase):

    def setUp(self):
        super(RestartOnExitTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()
        # The record as the sweep loaded it: found exited.
        self.container = mock.Mock(
            uuid='u-1', container_id='old-id', task_state=None,
            status=consts.STOPPED, status_detail='exit:1', exit_code=1,
            status_reason=None, healthcheck={})
        # A container of its own is its own sandbox owner: the sweep hands
        # the same object in both roles.
        self.capsule = self.container
        # The record as the database holds it now. A show usually sees the
        # exit before the sweep does and writes STOPPED, so that is the
        # ordinary shape; what matters is the mark, not the status.
        self.fresh = mock.Mock(status=consts.STOPPED, task_state=None,
                               status_detail='exit:1')
        # The fresh read must be the any-type getter: the sweep hands this
        # method capsule members (TYPE_CAPSULE_CONTAINER), and the typed
        # Container.get_by_uuid cannot see those -- it raised
        # ContainerNotFound for every capsule member and the restart policy
        # silently never applied to capsules. This mock was on get_by_uuid
        # once, which is exactly how that stayed green.
        self._patch(mock.patch.object(
            cri_driver.objects.Container, 'get_container_any_type',
            return_value=self.fresh))
        self._patch(mock.patch.object(
            cri_driver.objects.Container, 'get_by_uuid',
            side_effect=exception.ContainerNotFound(container='u-1')))
        self._patch(mock.patch.object(cri_driver.lockutils, 'lock'))
        self.restarted = self._patch(mock.patch.object(
            self.driver, '_restart_exited'))
        self.capsule_restarted = self._patch(mock.patch.object(
            self.driver, '_restart_container', return_value=True))
        self._patch(mock.patch.object(self.driver, '_record_start'))

    def _patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _policy(self, name, count=0):
        self.container.restart_policy = {'Name': name,
                                         'MaximumRetryCount': str(count)}

    def test_always_restarts_a_container_that_died(self):
        self._policy('always')

        self.assertTrue(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

        self.restarted.assert_called_once_with({}, self.container)
        self.capsule_restarted.assert_not_called()
        self.assertEqual(consts.RUNNING, self.container.status)
        self.assertIsNone(self.container.status_detail)
        self.assertEqual(
            1, self.container.healthcheck['k8s_probe_state']['restarts'])
        self.container.save.assert_called_once()

    def test_a_capsule_member_is_restarted_in_its_capsule(self):
        """The capsule owns the sandbox; the rebuild the probes use knows
        how to find it there."""
        self._policy('always')
        capsule = mock.Mock(uuid='cap-1', containers=[self.container])

        self.assertTrue(self.driver._restart_on_exit(
            {}, capsule, self.container))

        self.capsule_restarted.assert_called_once_with({}, capsule,
                                                       self.container)
        self.restarted.assert_not_called()

    def test_unless_stopped_is_always_here(self):
        """They differ only across a daemon restart, which has no
        equivalent on this driver; a stopped container never gets here."""
        self._policy('unless-stopped')

        self.assertTrue(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_no_policy_leaves_it_dead(self):
        self.container.restart_policy = None

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))
        self.restarted.assert_not_called()

    def test_the_no_policy_leaves_it_dead(self):
        self._policy('no')

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_on_failure_ignores_a_clean_exit(self):
        self._policy('on-failure')
        self.container.exit_code = 0

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_on_failure_restarts_a_failure(self):
        self._policy('on-failure')

        self.assertTrue(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_on_failure_stops_at_the_retry_count(self):
        """Two restarts used of two allowed: the third death is final."""
        self._policy('on-failure', count=2)
        self.container.healthcheck = {'k8s_probe_state': {'restarts': 2}}

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_spent_retries_are_said_once_on_the_record(self):
        """inspect can then say why it is not coming back; and the log is
        not told again every sweep for as long as it stays down."""
        self._policy('on-failure', count=2)
        self.container.healthcheck = {'k8s_probe_state': {'restarts': 2}}

        self.driver._restart_on_exit({}, self.capsule, self.container)
        self.driver._restart_on_exit({}, self.capsule, self.container)

        self.assertIn('used all 2', self.container.status_reason)
        self.assertIn('code 1', self.container.status_reason)
        self.container.save.assert_called_once()

    def test_on_failure_under_the_retry_count_restarts(self):
        self._policy('on-failure', count=2)
        self.container.healthcheck = {'k8s_probe_state': {'restarts': 1}}

        self.assertTrue(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_on_failure_zero_means_no_cap(self):
        self._policy('on-failure', count=0)
        self.container.healthcheck = {'k8s_probe_state': {'restarts': 50}}

        self.assertTrue(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_an_exit_whose_code_is_unknown_is_not_a_failure(self):
        """Restarting on an unknown code would loop on a runtime that
        cannot be asked."""
        self._policy('on-failure')
        self.container.exit_code = None

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_a_stop_that_landed_first_wins(self):
        """The owner stopped it since the sweep read the record."""
        self._policy('always')
        self.fresh.status_detail = cri_driver.CriDriver.STOPPED_BY_OWNER

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))
        self.restarted.assert_not_called()

    def test_an_operation_in_flight_wins(self):
        """A stop or delete holding the record owns what happens next."""
        self._policy('always')
        self.fresh.task_state = consts.CONTAINER_STOPPING

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_the_sweeps_own_copy_mid_operation_is_left_alone(self):
        self._policy('always')
        self.container.task_state = consts.CONTAINER_REBOOTING

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))
        self.restarted.assert_not_called()

    def test_a_start_that_landed_first_wins(self):
        """The owner started it themselves since the sweep read it."""
        self._policy('always')
        self.fresh.status = consts.RUNNING

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_a_delete_in_progress_wins(self):
        self._policy('always')
        self.fresh.status = consts.DELETING

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_a_record_that_is_gone_is_not_restarted(self):
        self._policy('always')
        cri_driver.objects.Container.get_container_any_type.side_effect = (
            exception.ContainerNotFound(container='u-1'))

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))

    def test_a_failed_restart_leaves_the_death_on_the_record(self):
        """Saying it restarted when it did not is the silent failure this
        exists to remove."""
        self._policy('always')
        self.restarted.side_effect = exception.ZunException('sandbox gone')

        self.assertFalse(self.driver._restart_on_exit(
            {}, self.capsule, self.container))
        self.assertEqual(consts.STOPPED, self.container.status)
        self.assertEqual('exit:1', self.container.status_detail)


class OwnerStopIsMarkedTest(base.TestCase):
    """The runtime reports an owner's stop and a death the same way."""

    def setUp(self):
        super(OwnerStopIsMarkedTest, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver.runtime_stub = mock.Mock()

    def test_stop_marks_the_record(self):
        container = mock.Mock(container_id='c-1', status=consts.RUNNING,
                              status_detail=None)

        self.driver.stop({}, container, timeout=5)

        self.assertEqual(consts.STOPPED, container.status)
        self.assertEqual(cri_driver.CriDriver.STOPPED_BY_OWNER,
                         container.status_detail)

    def test_the_mark_is_not_overwritten_by_the_exit_code(self):
        """SIGKILL's 137 would otherwise read as a crash."""
        container = mock.Mock(
            container_id='c-1',
            status_detail=cri_driver.CriDriver.STOPPED_BY_OWNER)

        self.driver._record_exit(container)

        self.driver.runtime_stub.ContainerStatus.assert_not_called()
        self.assertEqual(cri_driver.CriDriver.STOPPED_BY_OWNER,
                         container.status_detail)

    def test_the_mark_is_cleared_when_it_runs_again(self):
        container = mock.Mock(
            container_id='c-1', status=consts.STOPPED, started_at=None,
            status_detail=cri_driver.CriDriver.STOPPED_BY_OWNER)
        response = mock.Mock(
            state=cri_driver.api_pb2.ContainerState.CONTAINER_RUNNING)
        with mock.patch.object(self.driver, '_record_start'):
            self.driver._populate_container_state(container, response)

        self.assertEqual(consts.RUNNING, container.status)
        self.assertIsNone(container.status_detail)
