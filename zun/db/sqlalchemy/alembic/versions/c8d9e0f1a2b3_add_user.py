"""add user to container

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-31

"""

from alembic import op
import sqlalchemy as sa

revision = 'c8d9e0f1a2b3'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade():
    # Which user a container's process runs as. Without a field for it a
    # container asking to drop to an unprivileged user ran as root and
    # nothing said so -- a request about the container's own privilege,
    # answered with the opposite, in silence.
    #
    # Held as docker writes it (`uid`, `uid:gid`, `name`, `name:group`)
    # rather than split into numbers: a name only the image can resolve
    # is a valid answer here, and splitting it would mean guessing which
    # half of the pair is which for a form this platform cannot resolve.
    op.add_column('container',
                  sa.Column('user', sa.String(length=255), nullable=True))
