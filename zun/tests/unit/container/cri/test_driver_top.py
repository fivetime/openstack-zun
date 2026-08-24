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

"""One API, one answer, whichever driver served it.

The api-ref fixes top's response as {"Titles": [...], "Processes":
[[...]]}. Returning ps output raw left every consumer -- the CLI, the
dashboard, anything built on the API -- to guess the shape from whatever
it happened to receive.
"""

from zun.container.cri import driver
from zun.tests import base


class TestProcessTable(base.TestCase):

    def test_the_shape_the_api_ref_documents(self):
        self.assertEqual(
            {'Titles': ['PID', 'USER', 'COMMAND'],
             'Processes': [['1', 'root', 'sh'], ['9', 'root', 'ps -ef']]},
            driver._as_process_table('PID   USER     COMMAND\n'
                                     '    1 root     sh\n'
                                     '    9 root     ps -ef\n'))

    def test_the_command_keeps_its_spaces(self):
        """The last column is a command line, not one word."""
        table = driver._as_process_table(
            'PID CMD\n1 sh -c "echo a; echo b"\n')

        self.assertEqual([['1', 'sh -c "echo a; echo b"']],
                         table['Processes'])

    def test_headings_only_means_no_processes(self):
        self.assertEqual({'Titles': ['PID', 'CMD'], 'Processes': []},
                         driver._as_process_table('PID CMD\n'))

    def test_nothing_at_all_is_an_empty_table(self):
        self.assertEqual({'Titles': [], 'Processes': []},
                         driver._as_process_table(''))
