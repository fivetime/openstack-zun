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

"""add docker create options to container (API 1.53)

Options docker takes at create and this API had no field for. The
gateway in front of it refused each one by name; they are carried now,
and a driver that cannot apply one refuses the create instead of
dropping it. All nullable: absent means the runtime's default, as
before.

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-09-17 10:00:00.000000

"""

revision = 'e1f2a3b4c5d6'
down_revision = 'd9e0f1a2b3c4'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa

from zun.db.sqlalchemy import models

_COLUMNS = (
    ('dns_options', models.JSONEncodedList),
    ('extra_hosts', models.JSONEncodedList),
    ('ulimits', models.JSONEncodedList),
    # MiB, the unit memory is in.
    ('shm_size', sa.Integer),
    # The root filesystem, not a mount (mounts carry their own).
    ('read_only', sa.Boolean),
    ('init', sa.Boolean),
    ('group_add', models.JSONEncodedList),
    ('oom_score_adj', sa.Integer),
    # path -> mount options
    ('tmpfs', models.JSONEncodedDict),
)


def upgrade():
    for name, kind in _COLUMNS:
        op.add_column('container', sa.Column(name, kind(), nullable=True))


def downgrade():
    for name, _kind in reversed(_COLUMNS):
        op.drop_column('container', name)
