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

"""The runtime's healthcheck verdict reaches the container record.

A healthcheck could be configured on create and the runtime ran it,
but nothing read the verdict back: State.Health was dropped when the
container's state was populated. A caller waiting for a dependency to
become healthy -- `depends_on: condition: service_healthy` -- had
nothing to wait on and gave up with "no healthcheck configured".
"""

from unittest import mock

from zun.container.docker import driver as docker_driver
from zun.tests import base


def _populate(state):
    driver = docker_driver.DockerDriver.__new__(docker_driver.DockerDriver)
    container = mock.Mock(task_state=None)
    driver._populate_container_state(container, state, force=True)
    return container


class TestHealthIsRead(base.TestCase):

    def test_the_verdict_is_kept_in_dockers_own_words(self):
        for verdict in ('starting', 'healthy', 'unhealthy'):
            container = _populate({'Status': 'running', 'Running': True,
                                   'StartedAt': '2026-08-29T00:00:00Z',
                                   'Health': {'Status': verdict}})
            self.assertEqual(verdict, container.health)

    def test_no_healthcheck_reads_as_none_not_a_word(self):
        container = _populate({'Status': 'running', 'Running': True,
                               'StartedAt': '2026-08-29T00:00:00Z'})

        self.assertIsNone(container.health)

    def test_it_is_read_on_a_stopped_container_too(self):
        """Health has a last verdict; a stop does not erase it."""
        container = _populate({'Status': 'exited', 'Running': False,
                               'Paused': False, 'Restarting': False,
                               'Error': '', 'ExitCode': 0,
                               'StartedAt': '2026-08-29T00:00:00Z',
                               'FinishedAt': '2026-08-29T00:01:00Z',
                               'Health': {'Status': 'unhealthy'}})

        self.assertEqual('unhealthy', container.health)
