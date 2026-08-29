"""add health to container

Revision ID: b7c8d9e0f1a2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-29

"""

from alembic import op
import sqlalchemy as sa

revision = 'b7c8d9e0f1a2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    # The runtime runs the healthcheck a container was created with and
    # keeps a verdict -- starting, healthy, unhealthy -- and nothing here
    # read it back. A caller waiting for a dependency to be ready had
    # nothing to wait on, so a healthcheck could be configured and never
    # consulted.
    op.add_column('container',
                  sa.Column('health', sa.String(length=16), nullable=True))
