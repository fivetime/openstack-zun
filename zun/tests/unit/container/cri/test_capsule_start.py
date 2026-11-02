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

"""A capsule starts the way kubelet starts a pod.

A failed init container follows restartPolicy with back-off, and an init
container with restartPolicy Always is a sidecar: it only has to have started,
runs beside everything after it, and does not decide the capsule's fate.
"""

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.container.cri import driver as cri_driver
from zun.tests import base


def _member(uuid, policy='on-failure', **kwargs):
    member = mock.Mock(uuid=uuid, status_detail=None, status=consts.CREATING,
                       container_id='id-' + uuid, healthcheck={},
                       restart_policy={'Name': policy}, **kwargs)
    member.name = uuid
    return member


class CapsuleStartBase(base.TestCase):

    def setUp(self):
        super(CapsuleStartBase, self).setUp()
        self.driver = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        self.driver._abandoned = set()
        self.driver.runtime_stub = mock.Mock()
        self.context = mock.Mock()
        self.main = _member('main', policy='always')
        self.init = _member('init')
        self.capsule = mock.Mock(uuid='c-1', container_id=None,
                                 addresses=None, containers=[self.main],
                                 init_containers=[self.init],
                                 status_reason=None,
                                 restart_policy={'Name': 'always'})
        self.create_container = self._patch('_create_container')
        self._patch('_create_pod_sandbox')
        self.undo = self._patch('_undo_create')
        self._patch('_record_start')
        self.show = self._patch('_show_container')
        sleep = mock.patch('time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def _patch(self, name, **kwargs):
        patcher = mock.patch.object(cri_driver.CriDriver, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _create(self):
        return self.driver.create_capsule(self.context, self.capsule, {},
                                          [{'network': 'net-1'}], {})

    def _exits(self, *details):
        """Each look at the init container finds its next run's exit."""
        left = list(details)

        def show(context, container):
            container.status = consts.STOPPED
            if container.status_detail is None and left:
                container.status_detail = left.pop(0)
            return container
        self.show.side_effect = show

    def _naps(self):
        return sum(c.args[0] for c in self.sleep.call_args_list)


class InitRetryTest(CapsuleStartBase):

    def test_never_fails_the_create_at_the_first_failure(self):
        self.capsule.restart_policy = {'Name': 'no'}
        self._exits('exit:3')
        exc = self.assertRaises(exception.ZunException, self._create)
        # kubezun reads the code out of this sentence.
        self.assertIn('Init container init failed with exit:3', str(exc))
        self.assertEqual(1, self.create_container.call_count)
        self.undo.assert_called_once()

    def test_the_failed_member_is_recorded_as_stopped(self):
        self.capsule.restart_policy = {'Name': 'no'}
        self._exits('exit:3')
        self.assertRaises(exception.ZunException, self._create)
        self.assertEqual(consts.STOPPED, self.init.status)
        self.init.save.assert_called()

    def test_on_failure_and_always_run_it_again(self):
        for policy in ('always', 'on-failure'):
            self.create_container.reset_mock()
            self.init.status_detail = None
            self.capsule.restart_policy = {'Name': policy}
            self._exits('exit:1', 'exit:0')
            capsule = self._create()
            self.assertEqual(consts.RUNNING, capsule.status)
            # Init twice, main once.
            self.assertEqual(3, self.create_container.call_count)

    def test_the_wait_doubles_up_to_kubelets_limit(self):
        self.assertEqual([10, 20, 40, 80, 160, 300, 300],
                         [cri_driver._init_backoff(n) for n in range(1, 8)])

    def test_it_says_why_while_it_waits_and_forgets_when_it_works(self):
        said = []
        self.capsule.save.side_effect = (
            lambda c: said.append(self.capsule.status_reason))
        self._exits('exit:7', 'exit:0')
        self._create()
        self.assertIn('Init container init failed with exit:7; trying again '
                      'in 10 seconds', said)
        self.assertIsNone(self.capsule.status_reason)

    def test_a_retry_is_a_new_attempt_of_a_removed_container(self):
        self._exits('exit:1', 'exit:0')
        self._create()
        self.assertEqual(
            1, self.init.healthcheck['k8s_probe_state']['restarts'])
        self.driver.runtime_stub.RemoveContainer.assert_called_once()

    def test_a_run_is_not_judged_by_the_exit_of_the_one_before(self):
        # A rebuild: the record says exit:0, and this run fails before the
        # first look. Judged by the record, the pod would go on without it.
        self.capsule.restart_policy = {'Name': 'no'}
        self.init.status_detail = 'exit:0'
        self._exits('exit:9')
        self.assertRaises(exception.ZunException, self._create)

    def test_a_delete_interrupts_the_back_off(self):
        self._exits('exit:1')

        def delete_during_the_wait(seconds):
            self.driver.abandon_create('c-1')
        self.sleep.side_effect = delete_during_the_wait
        self.assertRaises(exception.CapsuleCreateAbandoned, self._create)
        self.assertEqual(cri_driver.INIT_CONTAINER_POLL_MAX, self._naps())
        self.assertEqual(1, self.create_container.call_count)

    def test_it_keeps_trying_however_long_it_takes(self):
        self._exits(*(['exit:1'] * 30 + ['exit:0']))
        self.assertEqual(consts.RUNNING, self._create().status)
        self.assertEqual(32, self.create_container.call_count)

    def test_a_runtime_blip_does_not_fail_the_wait(self):
        import grpc

        class Blip(grpc.RpcError):
            pass
        answers = [Blip()]

        def show(context, container):
            if answers:
                raise answers.pop()
            container.status = consts.STOPPED
            container.status_detail = 'exit:0'
            return container
        self.show.side_effect = show
        self.assertEqual(consts.RUNNING, self._create().status)


class SidecarStartTest(CapsuleStartBase):

    def setUp(self):
        super(SidecarStartTest, self).setUp()
        self.sidecar = _member('side', policy='always')
        self.capsule.init_containers = [self.sidecar, self.init]

    def _shows(self):
        def show(context, container):
            if container is self.init:
                container.status = consts.STOPPED
                container.status_detail = 'exit:0'
            return container
        self.show.side_effect = show

    def test_only_an_init_container_with_always_is_one(self):
        self.assertTrue(cri_driver._is_sidecar(self.sidecar))
        self.assertFalse(cri_driver._is_sidecar(self.init))

    def test_it_does_not_have_to_exit(self):
        self._shows()
        order = []
        self.create_container.side_effect = (
            lambda ctx, cap, member, *a, **k: order.append(member.uuid))
        self._create()
        self.assertEqual(['side', 'init', 'main'], order)
        self.assertEqual(consts.RUNNING, self.sidecar.status)

    def test_a_startup_probe_holds_the_next_member_back(self):
        self.sidecar.healthcheck = {'k8s_probes': {'startupProbe': {
            'periodSeconds': 1}}}
        self.sidecar.created_at = None
        self.sidecar.started_at = None
        self._shows()
        answers = [False, True]
        probe = self._patch('_run_probe',
                            side_effect=lambda *a: answers.pop(0))
        with mock.patch.object(cri_driver, 'time') as clock:
            clock.time.side_effect = [0, 20, 40]
            self._create()
        self.assertEqual(2, probe.call_count)
        self.assertTrue(self.sidecar.healthcheck['k8s_probe_state'][
            'startup_passed'])

    def test_failing_its_startup_probe_runs_it_again_under_never_too(self):
        self.capsule.restart_policy = {'Name': 'no'}
        self.sidecar.healthcheck = {'k8s_probes': {'startupProbe': {
            'failureThreshold': 1}}}
        self.sidecar.created_at = None
        self.sidecar.started_at = None
        self._shows()
        answers = [False, True]
        self._patch('_run_probe', side_effect=lambda *a: answers.pop(0))
        self._create()
        self.assertEqual(3, sum(
            1 for c in self.create_container.call_args_list
            if c.args[2] in (self.sidecar, self.main)))
        self.assertEqual(
            1, self.sidecar.healthcheck['k8s_probe_state']['restarts'])

    def test_it_is_restarted_while_a_later_init_container_runs(self):
        restart = self._patch('_restart_container', return_value=True)
        looks = []

        def show(context, container):
            if container is self.sidecar:
                container.status = consts.STOPPED
                container.status_detail = 'exit:1'
            elif not looks:
                looks.append(1)
                container.status = consts.RUNNING
            else:
                container.status = consts.STOPPED
                container.status_detail = 'exit:0'
            return container
        self.show.side_effect = show
        self._create()
        restart.assert_called_once_with(self.context, self.capsule,
                                        self.sidecar)
        self.assertEqual(consts.RUNNING, self.sidecar.status)

    def test_restarts_while_starting_back_off(self):
        restart = self._patch('_restart_container', return_value=True)
        self.sidecar.status = consts.STOPPED
        self.sidecar.healthcheck = {'k8s_probe_state': {
            'restarts': 2}}
        with mock.patch.object(cri_driver.time, 'time', return_value=1000):
            self.sidecar.healthcheck['k8s_probe_state']['last_restart'] = 990
            self.driver._tend_sidecars(self.context, self.capsule,
                                       [self.sidecar])
            restart.assert_not_called()
            self.sidecar.healthcheck['k8s_probe_state']['last_restart'] = 900
            self.driver._tend_sidecars(self.context, self.capsule,
                                       [self.sidecar])
            restart.assert_called_once()


class SidecarLifeTest(CapsuleStartBase):

    def setUp(self):
        super(SidecarLifeTest, self).setUp()
        self.first = _member('side-1', policy='always')
        self.second = _member('side-2', policy='always')
        self.first.status = self.second.status = consts.RUNNING
        self.capsule.init_containers = [self.first, self.init, self.second]
        self.capsule.status = consts.RUNNING
        self.main.status = consts.RUNNING
        self.main.status_detail = None
        self.init.status = consts.STOPPED
        self.show.side_effect = lambda context, container: container

    def _sweep(self):
        self.driver.update_containers_states(self.context, [self.capsule],
                                             mock.Mock())

    def test_the_sweep_restarts_one_that_exited(self):
        restart = self._patch('_restart_on_exit', return_value=True)
        self.first.status = consts.STOPPED
        self.first.status_detail = 'exit:1'
        self._sweep()
        restart.assert_called_once_with(self.context, self.capsule,
                                        self.first)

    def test_it_does_not_decide_the_capsules_status(self):
        self._patch('_restart_on_exit', return_value=False)
        self.first.status = consts.STOPPED
        self.first.status_detail = 'exit:1'
        self._sweep()
        self.assertEqual(consts.RUNNING, self.capsule.status)

    def test_its_probes_run(self):
        probes = self._patch('_check_probes')
        self.capsule.status = consts.RUNNING
        self.driver.check_probes(self.context, [self.capsule])
        self.assertEqual(
            [self.main, self.first, self.second],
            [c.args[2] for c in probes.call_args_list])

    def test_a_plain_init_container_is_left_alone(self):
        restart = self._patch('_restart_on_exit', return_value=False)
        self.init.status_detail = 'exit:0'
        self.first.status = consts.RUNNING
        self._sweep()
        restart.assert_not_called()

    def _main_finishes(self, policy, exit_code):
        self.main.restart_policy = policy
        self.main.status = consts.STOPPED
        self.main.exit_code = exit_code
        self.main.status_detail = 'exit:%d' % exit_code
        self._patch('_restart_on_exit', return_value=False)
        self._sweep()

    def _stopped(self):
        return [c.args[0].container_id for c in
                self.driver.runtime_stub.StopContainer.call_args_list]

    def test_they_stop_in_reverse_when_the_mains_have_finished(self):
        self._main_finishes({'Name': 'no'}, 0)
        self.assertEqual(['id-side-2', 'id-side-1'], self._stopped())
        self.assertEqual(self.driver.STOPPED_BY_OWNER,
                         self.first.status_detail)
        self.assertEqual(consts.STOPPED, self.capsule.status)

    def test_on_failure_with_a_clean_exit_has_finished(self):
        self._main_finishes({'Name': 'on-failure'}, 0)
        self.assertEqual(2, len(self._stopped()))

    def test_on_failure_with_a_failure_has_not(self):
        self._main_finishes({'Name': 'on-failure'}, 1)
        self.assertEqual([], self._stopped())

    def test_on_failure_with_its_retries_spent_has(self):
        self.main.healthcheck = {'k8s_probe_state': {'restarts': 2}}
        self._main_finishes({'Name': 'on-failure',
                             'MaximumRetryCount': '2'}, 1)
        self.assertEqual(2, len(self._stopped()))

    def test_always_has_not(self):
        self._main_finishes({'Name': 'always'}, 0)
        self.assertEqual([], self._stopped())

    def test_a_delete_stops_the_mains_first_and_sidecars_last(self):
        self.capsule.container_id = 'sb-1'
        self._patch('_delete_sandbox')
        self._patch('_delete_neutron_ports')
        self.driver.delete_capsule(self.context, self.capsule, False)
        self.assertEqual(['id-main', 'id-side-2', 'id-side-1'],
                         self._stopped())

    def test_a_forced_delete_does_not_wait_for_any_of_that(self):
        self.capsule.container_id = 'sb-1'
        self._patch('_delete_sandbox')
        self._patch('_delete_neutron_ports')
        self.driver.delete_capsule(self.context, self.capsule, True)
        self.assertEqual([], self._stopped())

    def test_without_sidecars_a_delete_is_what_it_was(self):
        self.capsule.container_id = 'sb-1'
        self.capsule.init_containers = [self.init]
        self._patch('_delete_sandbox')
        self._patch('_delete_neutron_ports')
        self.driver.delete_capsule(self.context, self.capsule, False)
        self.assertEqual([], self._stopped())


class ResumeTest(CapsuleStartBase):

    def test_no_sandbox_on_record_is_nothing_to_resume(self):
        self.capsule.container_id = None
        self.assertFalse(self.driver.resume_capsule(
            self.context, self.capsule, mock.Mock()))
        self.undo.assert_called_once()

    def test_a_sandbox_on_record_is_rebuilt_in_a_thread_of_its_own(self):
        self.capsule.container_id = 'sb-1'
        on_done = mock.Mock()
        with mock.patch.object(cri_driver.utils, 'spawn_worker') as spawn:
            self.assertTrue(self.driver.resume_capsule(
                self.context, self.capsule, on_done))
        spawn.assert_called_once_with(self.driver._rebuild_under_lock,
                                      self.context, 'c-1', on_done)

    def _resume(self, rebuilt):
        on_done = mock.Mock()
        fresh = mock.Mock(status=consts.CREATING, task_state='creating')
        self._patch('_rebuild_capsule', **rebuilt)
        with mock.patch.object(cri_driver.objects.Capsule, 'get_by_uuid',
                               return_value=fresh), \
                mock.patch.object(cri_driver.lockutils, 'lock'):
            self.driver._rebuild_under_lock(self.context, 'c-1', on_done)
        return on_done, fresh

    def test_a_resumed_create_says_how_it_ended(self):
        on_done, fresh = self._resume({})
        on_done.assert_called_once_with(fresh, None)

    def test_a_resumed_create_that_fails_leaves_nothing_behind(self):
        err = exception.ZunException('Init container x failed with exit:1')
        on_done, fresh = self._resume({'side_effect': err})
        self.undo.assert_called_once_with(self.context, fresh)
        on_done.assert_called_once_with(fresh, err)


class RunningInitRecordTest(CapsuleStartBase):

    def test_a_running_init_container_is_recorded_as_running(self):
        # What says the sandbox is up and a pod is initializing rather than
        # stuck, for as long as the init container takes.
        saved = []
        self.init.save.side_effect = lambda c: saved.append(self.init.status)
        self.show.side_effect = lambda c, m: setattr(
            m, 'status', consts.STOPPED) or setattr(
            m, 'status_detail', 'exit:0') or m
        self._create()
        self.assertEqual(consts.RUNNING, saved[0])
        self.assertEqual(consts.STOPPED, saved[-1])


class DeleteDuringThePullTest(base.TestCase):
    """A pull cannot be interrupted; what follows it can."""

    def test_the_container_is_not_made_once_the_pull_returns(self):
        drv = cri_driver.CriDriver.__new__(cri_driver.CriDriver)
        drv._abandoned = set()
        drv.runtime_stub = mock.Mock()
        capsule = mock.Mock(uuid='c-1')

        def delete_arrives(context, container):
            drv.abandon_create('c-1')

        with mock.patch.object(drv, '_pull_image',
                               side_effect=delete_arrives):
            self.assertRaises(exception.CapsuleCreateAbandoned,
                              drv._create_container, mock.Mock(), capsule,
                              mock.Mock(), [])

        drv.runtime_stub.CreateContainer.assert_not_called()
