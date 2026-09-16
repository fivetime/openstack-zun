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

"""add read_only to volume_mapping

Whether an attachment is mounted read-only. Until now every volume, and
every contents file bound into a container, was writable by the
container: a secret handed in as a file could be rewritten, and a file
bound from the node's volume directory could be grown until the node's
disk was full -- which reaches every container on that node. The bind
itself is where read-only is applied, so it is stored per attachment.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-09-17 00:30:00.000000

"""

# revision identifiers, used by Alembic.
revision = 'd9e0f1a2b3c4'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('volume_mapping',
                  sa.Column('read_only', sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column('volume_mapping', 'read_only')
