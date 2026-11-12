"""plex invites: when the welcome DM was sent

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-05 20:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
import woosarr.db.models


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('plex_invites') as batch_op:
        batch_op.add_column(sa.Column('welcomed_at', woosarr.db.models.UTCDateTime(), nullable=True))
    # Invites accepted before this version were never welcomed; don't send them one now.
    op.execute("UPDATE plex_invites SET welcomed_at = resolved_at WHERE status <> 'pending'")


def downgrade() -> None:
    with op.batch_alter_table('plex_invites') as batch_op:
        batch_op.drop_column('welcomed_at')
