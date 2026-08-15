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

"""add flavor limit columns

The BSS sizes a container from a flavor, and a flavor is more than cpu and
memory: it is also how many processes the container may fork, whether its
memory limit is allowed to spill into swap, and how hard it may hammer the
node's disk. None of those could be said per container before -- they were
either a global operator default or simply absent.

Revision ID: a1f5be2c7d90
Revises: 3f2b36231bee
Create Date: 2026-08-15 09:30:00.000000

"""

# revision identifiers, used by Alembic.
revision = 'a1f5be2c7d90'
down_revision = '3f2b36231bee'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('container',
                  sa.Column('pids_limit', sa.Integer(), nullable=True))
    op.add_column('container',
                  sa.Column('memory_swap', sa.Integer(), nullable=True))
    op.add_column('container',
                  sa.Column('blkio_weight', sa.Integer(), nullable=True))
    op.add_column('container',
                  sa.Column('device_read_bps', sa.BigInteger(),
                            nullable=True))
    op.add_column('container',
                  sa.Column('device_write_bps', sa.BigInteger(),
                            nullable=True))
    op.add_column('container',
                  sa.Column('device_read_iops', sa.Integer(), nullable=True))
    op.add_column('container',
                  sa.Column('device_write_iops', sa.Integer(), nullable=True))
