"""media requests (Seerr)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17 22:40:00.000000
"""

from alembic import op
import sqlalchemy as sa
import woosarr.db.models


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('media_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('seerr_request_id', sa.Integer(), nullable=False),
    sa.Column('discord_user_id', sa.String(length=32), nullable=False),
    sa.Column('plex_user_id', sa.String(length=64), nullable=True),
    sa.Column('seerr_user_id', sa.Integer(), nullable=False),
    sa.Column('media_type', sa.String(length=8), nullable=False),
    sa.Column('tmdb_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('year', sa.String(length=4), nullable=False),
    sa.Column('seasons', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('notified_status', sa.String(length=20), nullable=True),
    sa.Column('created_at', woosarr.db.models.UTCDateTime(), nullable=False),
    sa.Column('updated_at', woosarr.db.models.UTCDateTime(), nullable=False),
    sa.Column('checked_at', woosarr.db.models.UTCDateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('seerr_request_id')
    )
    with op.batch_alter_table('media_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_media_requests_discord_user_id'), ['discord_user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('media_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_media_requests_discord_user_id'))
    op.drop_table('media_requests')
