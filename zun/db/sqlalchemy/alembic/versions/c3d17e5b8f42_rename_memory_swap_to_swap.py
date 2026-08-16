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

"""rename memory_swap to swap

The column held what the runtime takes -- memory and swap as one total --
and so did every interface above it, which made the swap a thing callers
had to derive and re-derive. It is the swap now, and the total is worked
out where it is needed, in the driver that speaks to the runtime.

Storing the swap rather than the total also survives a memory change: an
update that raises the memory limit would otherwise leave a total behind
that silently means less swap, or none, or nothing coherent at all.

Revision ID: c3d17e5b8f42
Revises: b7c04e19af32
Create Date: 2026-08-16 02:10:00.000000

"""

# revision identifiers, used by Alembic.
revision = 'c3d17e5b8f42'
down_revision = 'b7c04e19af32'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    # The value stored so far was a total including the memory limit, and
    # there is no memory to subtract here that is guaranteed to be the one
    # it was written against. The column is a day old and carries nothing
    # in any deployment, so it is emptied rather than mistranslated.
    op.execute('UPDATE container SET memory_swap = NULL')
    op.alter_column('container', 'memory_swap',
                    new_column_name='swap',
                    existing_type=sa.Integer(),
                    existing_nullable=True)
