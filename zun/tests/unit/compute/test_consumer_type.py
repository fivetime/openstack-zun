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

from unittest import mock

from zun.common import consts
from zun.common import exception
from zun.compute import manager
import zun.conf
from zun import objects
from zun.scheduler.client import report
from zun.tests import base as test_base
from zun.tests.unit.db import base
from zun.tests.unit.db import utils

NOVA = 'aaaaaaaa-0000-4000-8000-000000000001'
OURS = 'aaaaaaaa-0000-4000-8000-000000000002'
UNTYPED = 'aaaaaaaa-0000-4000-8000-000000000003'
UNREADABLE = 'aaaaaaaa-0000-4000-8000-000000000004'


class ClaimTypeTest(test_base.BaseTestCase):

    def test_claim_says_whose_it_is(self):
        with mock.patch.object(report.SchedulerReportClient, '__init__',
                               return_value=None):
            client = report.SchedulerReportClient()
        current = mock.Mock(status_code=200)
        current.json.return_value = {'allocations': {},
                                     'consumer_generation': None}
        response = mock.Mock(status_code=204)
        with mock.patch.object(client, 'put', return_value=response) as put, \
                mock.patch.object(client, 'get', return_value=current):
            client.claim_resources(
                mock.Mock(global_id='req'), OURS,
                {'allocations': {'rp': {'resources': {'VCPU': 1}}}},
                'project', 'user', '1.31', consumer_generation=None)

        payload = put.call_args[0][1]
        self.assertEqual('ZUN', payload['consumer_type'])
        self.assertEqual('1.38', put.call_args[1]['version'])

    def test_type_is_read_from_placement(self):
        with mock.patch.object(report.SchedulerReportClient, '__init__',
                               return_value=None):
            client = report.SchedulerReportClient()
        response = mock.Mock(status_code=200)
        response.__bool__ = lambda s: True
        response.json.return_value = {'allocations': {},
                                      'consumer_type': 'INSTANCE'}
        with mock.patch.object(client, 'get', return_value=response) as get:
            self.assertEqual('INSTANCE', client.get_consumer_type(
                mock.Mock(global_id='req'), NOVA))
        self.assertEqual('1.38', get.call_args[1]['version'])


class SweepOnSharedHostTest(base.DbTestCase):
    """Nova's allocations sit beside ours, and only ours may be collected."""

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        zun.conf.CONF.set_override('host_shared_with_nova', True,
                                   group='compute')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.report = self.manager.reportclient
        rt = mock.Mock(rp_uuid='rp')
        p = mock.patch.object(self.manager, '_get_resource_tracker',
                              return_value=rt)
        p.start()
        self.addCleanup(p.stop)
        self.report.get_allocations_for_resource_provider.return_value = (
            mock.Mock(allocations={NOVA: {}, OURS: {}, UNTYPED: {},
                                   UNREADABLE: {}}))

        def consumer_type(ctx, consumer):
            if consumer == UNREADABLE:
                raise exception.ConsumerAllocationRetrievalFailed(
                    consumer_uuid=consumer, error='placement is down')
            return {NOVA: 'INSTANCE', OURS: 'ZUN',
                    UNTYPED: 'unknown'}[consumer]
        self.report.get_consumer_type.side_effect = consumer_type
        for cls in (objects.Container, objects.Capsule):
            p = mock.patch.object(cls, 'list_by_host', return_value=[])
            p.start()
            self.addCleanup(p.stop)

    def _sweep_twice(self):
        self.manager.reclaim_stale_allocations(self.context)
        self.manager.reclaim_stale_allocations(self.context)

    def test_only_ours_is_collected(self):
        self._sweep_twice()

        self.report.delete_allocation_for_container.assert_called_once_with(
            mock.ANY, OURS)

    def test_a_host_of_our_own_needs_no_reading(self):
        zun.conf.CONF.set_override('host_shared_with_nova', False,
                                   group='compute')

        self._sweep_twice()

        self.report.get_consumer_type.assert_not_called()
        self.assertEqual(
            4, self.report.delete_allocation_for_container.call_count)

    def test_one_sweep_takes_nothing(self):
        """A capsule being scheduled is claimed before it has a host."""
        self.manager.reclaim_stale_allocations(self.context)

        self.report.delete_allocation_for_container.assert_not_called()

    def test_a_consumer_that_found_its_host_is_kept(self):
        self.manager.reclaim_stale_allocations(self.context)
        capsule = mock.Mock(uuid=OURS, containers=[])
        with mock.patch.object(objects.Capsule, 'list_by_host',
                               return_value=[capsule]):
            self.manager.reclaim_stale_allocations(self.context)

        self.report.delete_allocation_for_container.assert_not_called()

    def test_a_failed_release_is_tried_again(self):
        self.report.delete_allocation_for_container.side_effect = [
            exception.AllocationDeleteFailed(consumer_uuid=OURS, error='x'),
            None]
        self._sweep_twice()
        self.manager.reclaim_stale_allocations(self.context)

        self.assertEqual(
            2, self.report.delete_allocation_for_container.call_count)


class DeleteAfterTheSandboxIsGoneTest(base.DbTestCase):
    """Once the sandbox is gone, nothing may keep the record behind."""

    def setUp(self):
        super().setUp()
        zun.conf.CONF.set_override('container_driver', 'fake')
        with mock.patch(
                'zun.scheduler.client.report.SchedulerReportClient'):
            self.manager = manager.Manager()
        self.driver = mock.Mock()
        self.manager.capsule_driver = self.driver
        self.rt = mock.Mock()
        p = mock.patch.object(self.manager, '_get_resource_tracker',
                              return_value=self.rt)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.manager, '_detach_volumes')
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.manager, '_last_reading', return_value=None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(objects.Capsule, 'save')
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(objects.Capsule, 'destroy')
        self.destroy = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(objects.VolumeMapping, 'list_by_container',
                              return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self.capsule = objects.Capsule(
            self.context, **utils.get_test_container())
        self.capsule.status = consts.DELETING
        self.capsule.containers = []
        self.capsule.init_containers = []

    def test_conflicting_inventory_does_not_keep_the_record(self):
        self.rt.remove_usage_from_container.side_effect = (
            exception.ResourceProviderUpdateConflict(
                uuid='rp', generation=1, error='409'))

        self.manager._do_container_delete(self.context, self.capsule, True)

        self.destroy.assert_called_once_with(self.context)

    def test_failed_release_does_not_keep_the_record(self):
        # The sweep collects it: the claim names this service as its owner.
        self.manager.reportclient.delete_allocation_for_container.side_effect \
            = exception.AllocationDeleteFailed(consumer_uuid='x', error='503')

        self.manager._do_container_delete(self.context, self.capsule, True)

        self.destroy.assert_called_once_with(self.context)

    def test_failing_runtime_still_keeps_the_record(self):
        # The sandbox may be running: there is something left to delete.
        self.driver.delete_capsule.side_effect = RuntimeError('containerd')

        self.assertRaises(RuntimeError, self.manager._do_container_delete,
                          self.context, self.capsule, False)

        self.destroy.assert_not_called()
