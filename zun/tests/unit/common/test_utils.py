#    Copyright 2016 IBM, Corp.
#
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

import threading
from unittest import mock

from oslo_context import context as common_context

from zun.common import exception
from zun.common import utils
from zun.common.utils import check_container_id
from zun.common.utils import translate_exception
import zun.conf
from zun import objects
from zun.objects.container import Container
from zun.tests import base
from zun.tests.unit.db import utils as db_utils
from zun.tests.unit.objects import utils as obj_utils


CONF = zun.conf.CONF


class TestUtils(base.TestCase):
    """Test cases for zun.common.utils"""

    def test_check_container_id(self):

        @check_container_id
        def foo(self, context, container):
            pass

        fake_container = mock.MagicMock()
        fake_container.container_id = None

        self.assertRaises(exception.Invalid, foo,
                          self, self.context, fake_container)

    def test_translate_exception(self):

        @translate_exception
        def foo(self, context):
            raise TypeError()

        self.assertRaises(exception.ZunException, foo,
                          self, mock.MagicMock())

    def test_parse_image_name(self):
        self.assertEqual(('test', 'latest'),
                         utils.parse_image_name('test:latest'))
        self.assertEqual(('test', 'latest'),
                         utils.parse_image_name('test'))
        self.assertEqual(('test', 'test'),
                         utils.parse_image_name('test:test'))
        self.assertEqual(('test-test', 'test'),
                         utils.parse_image_name('test-test:test'))

    def test_parse_image_name_with_default_registry(self):
        CONF.set_override('default_registry', 'test.io', group='docker')
        self.assertEqual(('test.io/test', 'latest'),
                         utils.parse_image_name('test'))
        self.assertEqual(('test.io/test/test', 'latest'),
                         utils.parse_image_name('test/test'))
        self.assertEqual(('other.com/test/test', 'latest'),
                         utils.parse_image_name('other.com/test/test'))

    def test_parse_image_name_with_custom_registry(self):
        registry = obj_utils.get_test_registry(self.context, domain='test.io')
        self.assertEqual(('test.io/test', 'latest'),
                         utils.parse_image_name('test', registry=registry))
        self.assertEqual(('test.io/test/test', 'latest'),
                         utils.parse_image_name('test/test',
                                                registry=registry))
        self.assertEqual(('other.com/test/test', 'latest'),
                         utils.parse_image_name('other.com/test/test',
                                                registry=registry))

    def test_get_image_pull_policy(self):
        self.assertEqual('always',
                         utils.get_image_pull_policy('always',
                                                     'latest'))
        self.assertEqual('always',
                         utils.get_image_pull_policy(None,
                                                     'latest'))
        self.assertEqual('always',
                         utils.get_image_pull_policy(None,
                                                     '2.0'))

    def test_should_pull_image(self):
        self.assertFalse(utils.should_pull_image('never', True))
        self.assertFalse(utils.should_pull_image('never', False))
        self.assertTrue(utils.should_pull_image('always', True))
        self.assertTrue(utils.should_pull_image('always', False))
        self.assertTrue(utils.should_pull_image('ifnotpresent', False))
        self.assertFalse(utils.should_pull_image('ifnotpresent', True))

    def test_validate_container_state(self):
        container = Container(self.context, **db_utils.get_test_container())
        container.status = 'Stopped'
        with self.assertRaisesRegex(exception.InvalidStateException,
                                    "%s" % container.uuid):
            utils.validate_container_state(container, 'stop')
        with self.assertRaisesRegex(exception.InvalidStateException,
                                    "%s" % container.uuid):
            utils.validate_container_state(container, 'pause')
        container.status = 'Running'
        with self.assertRaisesRegex(exception.InvalidStateException,
                                    "%s" % container.uuid):
            utils.validate_container_state(container, 'start')
        with self.assertRaisesRegex(exception.InvalidStateException,
                                    "%s" % container.uuid):
            utils.validate_container_state(container, 'unpause')
        with self.assertRaisesRegex(exception.InvalidStateException,
                                    "%s" % container.uuid):
            utils.validate_container_state(container, 'delete')
        self.assertIsNone(utils.validate_container_state(
            container, 'reboot'))
        container.status = 'Stopped'
        self.assertIsNone(utils.validate_container_state(
            container, 'reboot'))
        container.status = 'Running'
        self.assertIsNone(utils.validate_container_state(
            container, 'execute'))

    @mock.patch('zun.common.clients.OpenStackClients.neutron')
    def test_get_security_group_ids(self, mock_neutron_client):
        security_groups = None
        self.assertIsNone(utils.get_security_group_ids(self.context,
                                                       security_groups))
        security_groups = ['test_security_group_name']
        neutron_client_instance = mock.MagicMock()
        neutron_client_instance.list_security_groups.return_value = \
            {'security_groups': [{'id': 'test_security_group_id',
                                 'name': 'test_security_group_name'}]}
        mock_neutron_client.return_value = neutron_client_instance
        security_group_ids = utils.get_security_group_ids(self.context,
                                                          security_groups)
        self.assertEqual(['test_security_group_id'], security_group_ids)
        security_groups = ["not_attached_security_group_name"]
        self.assertRaises(exception.ZunException, utils.get_security_group_ids,
                          self.context, security_groups)

    def test_capsule_get_container_spec(self):
        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate,
                "Capsule need to have one container at least"):
            params = ({"containers": []})
            utils.capsule_get_container_spec(params)

        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate, "Container "
                                                  "image is needed"):
            params = ({"containers": [{"labels": {"app": "web"}}]})
            utils.capsule_get_container_spec(params)

        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate, "Container image is needed"):
            params = ({"containers": [
                {"image": "test1"},
                {"environment": {"ROOT_PASSWORD": "foo0"}}]})
            utils.capsule_get_container_spec(params)

        params = ({"containers": [
            {"image": "test1", "env": {"ROOT_PASSWORD": "foo0"}}]})
        utils.capsule_get_container_spec(params)
        self.assertEqual(params.get("containers")[0].get("environment"),
                         {"ROOT_PASSWORD": "foo0"})
        self.assertNotIn("env", params.get("containers"))

    def test_capsule_get_volume_spec(self):
        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate,
                "Volume name is needed"):
            params = ({"volumes": [{"foo": "bar"}]})
            utils.capsule_get_volume_spec(params)

        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate, "Volume size is needed"):
            params = ({"volumes": [{"name": "test",
                                    "cinder": {"foo": "bar"}}]})
            utils.capsule_get_volume_spec(params)

        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate, "Volume size and uuid "
                                                  "could not be set at "
                                                  "the same time"):
            params = ({"volumes": [{"name": "test",
                                    "cinder": {"size": 3,
                                               "volumeID": "fakevolid"}}]})
            utils.capsule_get_volume_spec(params)

        with self.assertRaisesRegex(
                exception.InvalidCapsuleTemplate,
                "A capsule volume needs a cinder, file, emptyDir or nfs "
                "source"):
            params = ({"volumes": [{"name": "test", "other": {}}]})
            utils.capsule_get_volume_spec(params)

    @mock.patch.object(objects.ContainerActionEvent, 'event_start')
    @mock.patch.object(objects.ContainerActionEvent, 'event_finish')
    def test_wart_container_event(self, mock_finish, mock_start):
        container = Container(self.context, **db_utils.get_test_container())

        @utils.wrap_container_event(prefix='compute')
        def fake_event(self, context, container):
            pass

        fake_event(self, self.context, container=container)

        self.assertTrue(mock_start.called)
        self.assertTrue(mock_finish.called)

    @mock.patch.object(objects.ContainerActionEvent, 'event_start')
    @mock.patch.object(objects.ContainerActionEvent, 'event_finish')
    def test_wrap_container_event_return(self, mock_finish, mock_start):
        container = Container(self.context, **db_utils.get_test_container())

        @utils.wrap_container_event(prefix='compute')
        def fake_event(self, context, container):
            return True

        retval = fake_event(self, self.context, container=container)

        self.assertTrue(retval)
        self.assertTrue(mock_start.called)
        self.assertTrue(mock_finish.called)

    @mock.patch.object(objects.ContainerActionEvent, 'event_start')
    @mock.patch.object(objects.ContainerActionEvent, 'event_finish')
    def test_wrap_container_event_log_exception(self, mock_finish, mock_start):
        container = Container(self.context, **db_utils.get_test_container())

        @utils.wrap_container_event(prefix='compute')
        def fake_event(self, context, container):
            raise exception.ZunException()

        self.assertRaises(exception.ZunException, fake_event,
                          self, self.context, container=container)

        self.assertTrue(mock_start.called)
        self.assertTrue(mock_finish.called)
        args, kwargs = mock_finish.call_args
        self.assertIsInstance(kwargs['exc_val'], exception.ZunException)


class TestSpawnN(base.TestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(self._drop_executor)
        p = mock.patch.object(utils.monkey_patch,
                              'concurrency_mode_threading', return_value=True)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _drop_executor():
        if utils._executor is not None:
            utils._executor.shutdown(wait=True)
            utils._executor = None

    def _run(self, func, *args, **kwargs):
        utils.spawn_n(func, *args, **kwargs)
        # Shutting the pool down waits for what it was given.
        self._drop_executor()

    def test_threading_runs_on_another_thread(self):
        seen = []
        self._run(lambda a, b=None: seen.append(
            (threading.get_ident(), a, b)), 1, b=2)
        self.assertEqual(1, len(seen))
        self.assertNotEqual(threading.get_ident(), seen[0][0])
        self.assertEqual((1, 2), seen[0][1:])

    def test_threading_carries_the_request_context(self):
        seen = []
        self.context.update_store()
        self._run(lambda: seen.append(common_context.get_current()))
        self.assertEqual([self.context], seen)

    def test_threading_logs_what_the_task_raises(self):
        def boom():
            raise ValueError('lost unless logged')

        with mock.patch.object(utils.LOG, 'exception') as log:
            self._run(boom)
        log.assert_called_once()
        self.assertIn('boom', str(log.call_args))

    def test_threading_pool_size_is_the_option(self):
        CONF.set_override('thread_pool_size', 3, group='compute')
        utils.spawn_n(lambda: None)
        self.assertEqual(3, utils._executor._max_workers)

    def test_threading_pool_is_made_once(self):
        utils.spawn_n(lambda: None)
        first = utils._executor
        utils.spawn_n(lambda: None)
        self.assertIs(first, utils._executor)

    def test_threading_does_not_leave_the_context_behind(self):
        CONF.set_override('thread_pool_size', 1, group='compute')
        seen = []
        self.context.update_store()
        utils.spawn_n(lambda: None)
        # Same pool thread, a task submitted with no current context.
        common_context._request_store.context = None
        utils.spawn_n(lambda: seen.append(common_context.get_current()))
        self._drop_executor()
        self.assertEqual([None], seen)

    def test_threading_warns_when_every_thread_is_busy(self):
        CONF.set_override('thread_pool_size', 1, group='compute')
        release = threading.Event()
        started = threading.Event()

        def hold():
            started.set()
            release.wait(10)

        with mock.patch.object(utils.LOG, 'warning') as warn:
            utils.spawn_n(hold)
            started.wait(10)
            warn.assert_not_called()
            utils.spawn_n(lambda: None)
            release.set()
        warn.assert_called_once()
        self.assertIn('busy', warn.call_args.args[0])

    @mock.patch('eventlet.spawn_n')
    def test_eventlet_uses_a_green_thread(self, green_spawn):
        utils.monkey_patch.concurrency_mode_threading.return_value = False
        utils.spawn_n(lambda: None)
        green_spawn.assert_called_once()
        self.assertIsNone(utils._executor)
