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

from zun.cmd import wsproxy
from zun.tests import base


class TestWsproxyMain(base.TestCase):

    @mock.patch.object(wsproxy.websocketproxy, 'ZunWebSocketProxy')
    @mock.patch.object(wsproxy.config, 'parse_args')
    @mock.patch.object(wsproxy.zun_service, 'prepare_service')
    @mock.patch('multiprocessing.set_start_method')
    def test_children_are_forked(self, set_start_method, prepare, parse,
                                 proxy):
        # Python 3.14 defaults to forkserver, which cannot hand websockify's
        # per-connection child the accepted socket.
        start_server = proxy.return_value.start_server

        def check_fork_first():
            set_start_method.assert_called_once_with('fork', force=True)
        start_server.side_effect = check_fork_first

        wsproxy.main()

        start_server.assert_called_once_with()
