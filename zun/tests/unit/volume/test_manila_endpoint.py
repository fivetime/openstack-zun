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

from keystoneauth1 import access
from keystoneauth1 import fixture
from keystoneauth1.identity import access as access_plugin
from keystoneauth1 import session as ks_session

import zun.conf
from zun.tests import base
from zun.volume import manila

CONF = zun.conf.CONF

PUBLIC = 'http://manila.example.org/v2'
INTERNAL = 'http://manila-api.internal:8786/v2'
OTHER_REGION = 'http://manila-api.region-two:8786/v2'


class ManilaEndpointTest(base.TestCase):
    """Which Manila endpoint the NFS driver calls, from a real catalog.

    The catalog and the lookup are keystoneauth's own, so what is tested is
    the interface and region actually reaching the catalog, not a mock's idea
    of them.
    """

    def setUp(self):
        super(ManilaEndpointTest, self).setUp()
        token = fixture.V3Token()
        svc = token.add_service('sharev2')
        svc.add_endpoint('public', PUBLIC, region='RegionOne')
        svc.add_endpoint('internal', INTERNAL, region='RegionOne')
        svc.add_endpoint('internal', OTHER_REGION, region='RegionTwo')
        plugin = access_plugin.AccessInfoPlugin(
            access.create(body=token, auth_token='t'))
        sess = ks_session.Session(auth=plugin)
        p = mock.patch.object(manila, '_session', return_value=sess)
        p.start()
        self.addCleanup(p.stop)

    def test_default_is_public(self):
        CONF.set_override('region_name', 'RegionOne', group='manila_client')
        self.assertEqual(PUBLIC, manila._endpoint(self.context))

    def test_internal_endpoint_type(self):
        CONF.set_override('endpoint_type', 'internalURL',
                          group='manila_client')
        CONF.set_override('region_name', 'RegionOne', group='manila_client')
        self.assertEqual(INTERNAL, manila._endpoint(self.context))

    def test_region(self):
        CONF.set_override('endpoint_type', 'internal', group='manila_client')
        CONF.set_override('region_name', 'RegionTwo', group='manila_client')
        self.assertEqual(OTHER_REGION, manila._endpoint(self.context))
