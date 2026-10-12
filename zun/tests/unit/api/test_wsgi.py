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

from zun.api import wsgi
from zun.tests import base


class TestInitApplication(base.TestCase):

    @mock.patch.object(wsgi.usage_listener, 'start')
    @mock.patch.object(wsgi.app, 'load_app')
    @mock.patch.object(wsgi.profiler, 'setup')
    @mock.patch.object(wsgi.service, 'prepare_service')
    @mock.patch.object(wsgi.CONF, 'reset')
    def test_listens_for_usage(self, reset, prepare, setup, load_app,
                               start):
        # Under a WSGI server nothing else starts the listener that
        # zun-api starts beside its workers.
        self.assertIs(load_app.return_value, wsgi.init_application())
        start.assert_called_once_with()
