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

"""add volume io limits

Ceph cannot cap a krbd mapping: rbd QoS lives in librbd, which the kernel
client never enters, and mclock schedules classes rather than images. The
only place a per-volume ceiling can be applied is the node, on the cgroup
of the container the volume is attached to -- so the ceiling is stored per
attachment.

Revision ID: b7c04e19af32
Revises: a1f5be2c7d90
Create Date: 2026-08-15 11:40:00.000000

"""

# revision identifiers, used by Alembic.
revision = 'b7c04e19af32'
down_revision = 'a1f5be2c7d90'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('volume_mapping',
                  sa.Column('read_bps', sa.BigInteger(), nullable=True))
    op.add_column('volume_mapping',
                  sa.Column('write_bps', sa.BigInteger(), nullable=True))
    op.add_column('volume_mapping',
                  sa.Column('read_iops', sa.Integer(), nullable=True))
    op.add_column('volume_mapping',
                  sa.Column('write_iops', sa.Integer(), nullable=True))
