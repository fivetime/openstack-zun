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

"""A measurement, held in a cache, never written to the database.

A figure that is lost is replaced by the next report; one that has aged
out reads as unknown. That is distinct from zero, and the distinction is
the point: zero says the container has written nothing, unknown says
nobody has measured it lately.
"""

from unittest import mock

from zun.api import usage_listener
from zun.common import usage_cache
from zun.tests import base


class UsageCacheTest(base.TestCase):

    def setUp(self):
        super(UsageCacheTest, self).setUp()
        usage_cache._REGION = None
        self.addCleanup(setattr, usage_cache, '_REGION', None)

    def test_what_was_remembered_is_recalled(self):
        usage_cache.remember('u1', {'size_rw': 42, 'measured_at': 't'})

        self.assertEqual({'size_rw': 42, 'measured_at': 't'},
                         usage_cache.recall('u1'))

    def test_nothing_heard_reads_as_none_not_zero(self):
        self.assertIsNone(usage_cache.recall('never-reported'))

    def test_many_at_once_omits_the_unknown(self):
        usage_cache.remember('u1', {'size_rw': 1})
        usage_cache.remember('u3', {'size_rw': 3})

        found = usage_cache.recall_many(['u1', 'u2', 'u3'])

        self.assertEqual({'u1': {'size_rw': 1}, 'u3': {'size_rw': 3}}, found)

    def test_asking_for_nothing_costs_nothing(self):
        with mock.patch.object(usage_cache, '_region') as region:
            self.assertEqual({}, usage_cache.recall_many([]))
        region.assert_not_called()

    def test_the_lifetime_follows_the_report_interval(self):
        """An operator tuning one must not have to remember the other."""
        self.config(report_interval=30, retain_reports=4, group='usage')

        usage_cache._region()

        self.assertEqual(120, usage_cache.CONF.cache.expiration_time)


class UsageEndpointTest(base.TestCase):

    def setUp(self):
        super(UsageEndpointTest, self).setUp()
        usage_cache._REGION = None
        self.addCleanup(setattr, usage_cache, '_REGION', None)
        self.endpoint = usage_listener.UsageEndpoint()

    def _hear(self, payload):
        return self.endpoint.info({}, 'zun.node1', 'container.usage',
                                  payload, {})

    def test_one_report_carries_a_whole_host(self):
        self._hear({'host': 'node1', 'measured_at': 't',
                    'containers': [{'uuid': 'a', 'size_rw': 10},
                                   {'uuid': 'b', 'size_rw': 20}]})

        self.assertEqual(10, usage_cache.recall('a')['size_rw'])
        self.assertEqual(20, usage_cache.recall('b')['size_rw'])
        self.assertEqual('node1', usage_cache.recall('a')['host'])

    def test_an_entry_without_a_uuid_is_skipped_not_fatal(self):
        self._hear({'host': 'n', 'measured_at': 't',
                    'containers': [{'size_rw': 1}, {'uuid': 'ok',
                                                    'size_rw': 2}]})

        self.assertEqual(2, usage_cache.recall('ok')['size_rw'])

    def test_only_usage_events_pass_the_filter(self):
        rule = usage_listener.UsageEndpoint.filter_rule
        self.assertTrue(rule.match(None, 'p', 'container.usage', {}, {}))
        self.assertFalse(rule.match(None, 'p', 'container.create.end', {},
                                    {}))
