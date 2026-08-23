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

from zun.db.sqlalchemy import api as dbapi
from zun.tests import base


class FakeContainer(object):
    def __init__(self, labels):
        self.labels = labels


def containers(*label_sets):
    return [FakeContainer(labels) for labels in label_sets]


class TestLabelFilter(base.TestCase):
    """Clients that group by label have nothing else to group by.

    docker compose finds its own containers this way, so without a filter
    it has to fetch every container in a project and throw most away.
    """

    def filter(self, given, labels):
        return dbapi.Connection._filter_by_labels(given, labels)

    def test_no_labels_asked_keeps_everything(self):
        given = containers({'a': '1'}, {})

        self.assertEqual(given, self.filter(given, None))

    def test_a_key_and_value(self):
        given = containers({'project': 'shop'}, {'project': 'other'})

        kept = self.filter(given, ['project=shop'])

        self.assertEqual([given[0]], kept)

    def test_a_key_alone_matches_any_value(self):
        given = containers({'traefik': 'true'}, {'other': 'x'})

        kept = self.filter(given, ['traefik'])

        self.assertEqual([given[0]], kept)

    def test_every_label_must_match(self):
        given = containers({'project': 'shop'},
                           {'project': 'shop', 'service': 'db'})

        kept = self.filter(given, ['project=shop', 'service=db'])

        self.assertEqual([given[1]], kept)

    def test_a_container_with_no_labels_is_not_a_match(self):
        given = containers({}, {'a': '1'})

        self.assertEqual([given[1]], self.filter(given, ['a=1']))

    def test_a_value_containing_an_equals_sign_survives(self):
        given = containers({'cmd': 'a=b'})

        self.assertEqual(given, self.filter(given, ['cmd=a=b']))

    def test_a_dict_may_be_given_directly(self):
        given = containers({'project': 'shop'}, {'project': 'other'})

        self.assertEqual([given[0]], self.filter(given, {'project': 'shop'}))
