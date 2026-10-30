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


"""add capsule_runtimes to compute_node

A host's runtimes column holds what the container driver offers, and a
capsule is created by the capsule driver, which on a docker host is a
different program with a different list: dockerd names its runtimes in
daemon.json, containerd's CRI names its handlers in its own config, and
the two need not agree (gVisor is runsc to one and gvisor to the other).
Scheduling a capsule against the container driver's list placed it on
hosts whose capsule driver did not have the runtime, and refused hosts
that did.

Revision ID: a7c3e9d2b4f1
Revises: f2a3b4c5d6e7
Create Date: 2026-10-30 12:00:00.000000

"""

revision = 'a7c3e9d2b4f1'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa

import zun


def upgrade():
    op.add_column('compute_node',
                  sa.Column('capsule_runtimes',
                            zun.db.sqlalchemy.models.JSONEncodedList(),
                            nullable=True))


def downgrade():
    op.drop_column('compute_node', 'capsule_runtimes')
