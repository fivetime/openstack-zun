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

"""add logs stream session

Kept apart from the attach session rather than sharing it. Both are a url
for the proxy to dial and a token that admits one caller, but a container
being followed while someone is attached to it is ordinary, and one slot
for two sessions means whichever started second turns the other's token
into a stranger's.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-23 18:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('container',
                  sa.Column('logs_url', sa.String(length=255),
                            nullable=True))
    op.add_column('container',
                  sa.Column('logs_token', sa.String(length=255),
                            nullable=True))
