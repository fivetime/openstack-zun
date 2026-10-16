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

"""The CRI driver makes its gRPC channel in the process that calls.

zun-compute builds the driver in its parent process and runs it in a forked
worker. With gRPC's fork support off, a process that has made a channel
leaves a forked child in which every call waits forever; a compute node
whose container driver was the CRI one hung in init_containers, before its
RPC server started.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from unittest import mock

from zun.container.cri import driver as cri_driver
from zun.tests import base


class LazyChannelTest(base.TestCase):

    def setUp(self):
        super(LazyChannelTest, self).setUp()
        p = mock.patch.object(cri_driver.grpc, 'insecure_channel')
        self.insecure_channel = p.start()
        self.addCleanup(p.stop)
        self.config(image_driver_list=[])

    def test_making_the_driver_makes_no_channel(self):
        cri_driver.CriDriver()

        self.insecure_channel.assert_not_called()

    def test_the_first_stub_used_makes_one_channel_for_all(self):
        driver = cri_driver.CriDriver()

        driver.runtime_stub
        for name in cri_driver._STUBS:
            getattr(driver, name)

        self.insecure_channel.assert_called_once_with(
            cri_driver.CONF.cri_containerd_address)

    def test_first_users_at_once_make_one_channel(self):
        def slow_channel(address):
            time.sleep(0.05)
            return mock.Mock()
        self.insecure_channel.side_effect = slow_channel
        driver = cri_driver.CriDriver()

        threads = [threading.Thread(target=getattr,
                                    args=(driver, 'image_stub'))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(1, self.insecure_channel.call_count)

    def test_a_stub_the_instance_has_is_kept(self):
        driver = cri_driver.CriDriver()
        watched = mock.Mock()
        driver.runtime_stub = watched

        driver.task_stub

        self.assertIs(watched, driver.runtime_stub)

    def test_an_unknown_attribute_is_still_missing(self):
        driver = cri_driver.CriDriver()

        self.assertRaises(AttributeError, getattr, driver, 'no_such_thing')
        self.insecure_channel.assert_not_called()


# A CRI runtime service answering ListPodSandbox on a unix socket, in a
# process of its own: the process that forks must not touch gRPC.
_SERVER = textwrap.dedent('''
    import sys
    from concurrent import futures
    import grpc
    from zun.criapi import api_pb2, api_pb2_grpc

    class Runtime(api_pb2_grpc.RuntimeServiceServicer):
        def ListPodSandbox(self, request, context):
            return api_pb2.ListPodSandboxResponse(
                items=[api_pb2.PodSandbox(id='answered')])

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    api_pb2_grpc.add_RuntimeServiceServicer_to_server(Runtime(), server)
    server.add_insecure_port('unix://' + sys.argv[1])
    server.start()
    print('ready', flush=True)
    server.wait_for_termination()
''')

# What zun-compute does: set the process up as zun does, make the driver
# in the parent, call it from a forked child.
_FORKER = textwrap.dedent('''
    import multiprocessing
    import sys
    from zun import monkey_patch
    monkey_patch.patch()
    import zun.conf
    from zun.container.cri import driver as cri_driver
    from zun.criapi import api_pb2

    zun.conf.CONF([], project='zun')
    zun.conf.CONF.set_override('cri_containerd_address',
                               'unix://' + sys.argv[1])
    zun.conf.CONF.set_override('image_driver_list', [])
    driver = cri_driver.CriDriver()

    def child(queue):
        items = driver.runtime_stub.ListPodSandbox(
            api_pb2.ListPodSandboxRequest(), timeout=5).items
        queue.put([item.id for item in items])

    multiprocessing.set_start_method('fork')
    queue = multiprocessing.Queue()
    proc = multiprocessing.Process(target=child, args=(queue,))
    proc.start()
    proc.join(20)
    if proc.is_alive():
        proc.kill()
        print('hung')
    else:
        print(queue.get() if not queue.empty() else 'exit %s' % proc.exitcode)
''')


class ForkedWorkerCallsTest(base.TestCase):

    def test_a_forked_worker_can_call_the_runtime(self):
        workdir = tempfile.mkdtemp()
        sock = os.path.join(workdir, 'cri.sock')
        env = dict(os.environ, OS_ZUN_DISABLE_EVENTLET_PATCHING='true')
        env.pop('GRPC_ENABLE_FORK_SUPPORT', None)
        server = subprocess.Popen([sys.executable, '-c', _SERVER, sock],
                                  stdout=subprocess.PIPE, env=env, text=True)
        self.addCleanup(server.wait)
        self.addCleanup(server.kill)
        self.assertEqual('ready', server.stdout.readline().strip())

        out = subprocess.run([sys.executable, '-c', _FORKER, sock],
                             capture_output=True, env=env, text=True,
                             timeout=60)

        self.assertEqual("['answered']", out.stdout.strip(), out.stderr)
