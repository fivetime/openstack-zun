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

import cotyledon

from zun.cni.daemon import service
from zun.tests import base


class TestCNIDaemonServiceManager(base.TestCase):

    @mock.patch.object(cotyledon.ServiceManager, 'register_hooks')
    @mock.patch.object(cotyledon.ServiceManager, 'add')
    @mock.patch.object(cotyledon.ServiceManager, '__init__',
                       return_value=None)
    def test_workers_are_forked(self, sm_init, add, hooks):
        # Python 3.14 defaults to forkserver; a worker in a fresh
        # interpreter has no parsed configuration and dies on its first
        # option. The registry's manager comes from the same context.
        with mock.patch('multiprocessing.context.ForkContext.Manager') as mgr:
            service.CNIDaemonServiceManager()

        ctx = sm_init.call_args.kwargs['mp_context']
        self.assertEqual('fork', ctx.get_start_method())
        mgr.assert_called_once_with()
        registry = mgr.return_value.dict.return_value
        add.assert_has_calls([
            mock.call(service.CNIDaemonWatcherService, workers=1,
                      args=(registry,)),
            mock.call(service.CNIDaemonServerService, workers=1,
                      args=(registry,))])
