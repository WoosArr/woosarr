"""error resolutions (dashboard)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-18 21:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
import woosarr.db.models


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('error_resolutions',
    sa.Column('fingerprint', sa.String(length=16), nullable=False),
    sa.Column('level', sa.String(length=10), nullable=False),
    sa.Column('logger', sa.String(length=100), nullable=False),
    sa.Column('message', sa.String(length=300), nullable=False),
    sa.Column('resolved_at', woosarr.db.models.UTCDateTime(), nullable=False),
    sa.Column('resolved_by', sa.String(length=100), nullable=False),
    sa.Column('until', woosarr.db.models.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('fingerprint')
    )


def downgrade() -> None:
    op.drop_table('error_resolutions')
