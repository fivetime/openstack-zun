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

"""add exit_code to container

Revision ID: d4e5f6a7b8c9
Revises: c3d17e5b8f42
Create Date: 2026-08-23

"""

from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d17e5b8f42'
branch_labels = None
depends_on = None


def upgrade():
    # docker has always reported what a container's process returned, and
    # callers script against it. Without somewhere to keep it, a container
    # that failed is indistinguishable from one that succeeded.
    op.add_column('container',
                  sa.Column('exit_code', sa.Integer(), nullable=True))
