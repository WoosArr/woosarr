"""discord member cache: is_bot

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows start as "not a bot"; the bot's full cache sync on (re)connect fills it in.
    with op.batch_alter_table('discord_members_cache') as batch_op:
        batch_op.add_column(sa.Column('is_bot', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('discord_members_cache') as batch_op:
        batch_op.drop_column('is_bot')
