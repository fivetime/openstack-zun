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

"""add a contents file's mode and owner to volume_mapping (API 1.54)

A file handed into a container as contents was written with whatever
mode and owner writing it gave -- 0644, root -- and a secret meant to be
read by an unprivileged user, or kept from others, could not be.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-17 12:30:00.000000

"""

revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa

_COLUMNS = (
    ('file_mode', sa.Integer),
    ('file_uid', sa.BigInteger),
    ('file_gid', sa.BigInteger),
)


def upgrade():
    for name, kind in _COLUMNS:
        op.add_column('volume_mapping',
                      sa.Column(name, kind(), nullable=True))


def downgrade():
    for name, _kind in reversed(_COLUMNS):
        op.drop_column('volume_mapping', name)
