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

"""add dns and dns_search to container

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-23

"""

from alembic import op
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    # A container that does not name its resolver has its DNS queries
    # forwarded from the host's network namespace, so they never traverse
    # the tenant's network and the names on it cannot be resolved. Both
    # columns are needed: a search domain alone does not move the query.
    op.add_column('container',
                  sa.Column('dns', sa.Text(), nullable=True))
    op.add_column('container',
                  sa.Column('dns_search', sa.Text(), nullable=True))
