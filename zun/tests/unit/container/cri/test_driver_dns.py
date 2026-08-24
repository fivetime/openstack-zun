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

"""Where a sandbox's resolvers come from.

The sandbox is built from a capsule on one path and from a container on
the other. Only the container carries `dns`/`dns_search`, the fields the
Container API grew for them, and reading annotations alone meant a
container's request was accepted and then dropped: the tenant asked for a
resolver, got no error, and found the host's resolv.conf inside their
container.
"""

from unittest import mock

from zun.container.cri import driver
from zun.tests import base


class TestDnsSources(base.TestCase):

    def test_a_containers_own_fields_are_used(self):
        container = mock.Mock(dns=['10.0.0.53'], dns_search=['corp.local'],
                              annotations=None)

        self.assertEqual(['10.0.0.53'], driver._dns_servers(container))
        self.assertEqual(['corp.local'], driver._dns_searches(container))

    def test_a_capsules_annotations_still_work(self):
        capsule = mock.Mock(
            spec=['annotations'],
            annotations={driver.DNS_SERVERS_ANNOTATION: '10.0.0.1, 10.0.0.2',
                         driver.DNS_SEARCHES_ANNOTATION: 'a.local,b.local'})

        self.assertEqual(['10.0.0.1', '10.0.0.2'],
                         driver._dns_servers(capsule))
        self.assertEqual(['a.local', 'b.local'], driver._dns_searches(capsule))

    def test_the_containers_own_fields_win_over_annotations(self):
        """Its own field is the more specific request of the two."""
        both = mock.Mock(
            dns=['10.0.0.53'], dns_search=[],
            annotations={driver.DNS_SERVERS_ANNOTATION: '10.0.0.1'})

        self.assertEqual(['10.0.0.53'], driver._dns_servers(both))

    def test_asking_for_neither_gives_neither(self):
        bare = mock.Mock(spec=[])

        self.assertEqual([], driver._dns_servers(bare))
        self.assertEqual([], driver._dns_searches(bare))
