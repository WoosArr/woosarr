"""family links: several Discord members may share one Plex account

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19 20:00:00.000000
"""

from alembic import op


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

# The 0001 unique constraints are unnamed; this convention names them on reflection.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    with op.batch_alter_table('discord_links', naming_convention=NAMING) as batch_op:
        batch_op.drop_constraint('uq_discord_links_plex_user_id', type_='unique')
        batch_op.create_index(batch_op.f('ix_discord_links_plex_user_id'), ['plex_user_id'], unique=False)


def downgrade() -> None:
    # Only possible while every Plex account has at most one linked member.
    with op.batch_alter_table('discord_links', naming_convention=NAMING) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discord_links_plex_user_id'))
        batch_op.create_unique_constraint('uq_discord_links_plex_user_id', ['plex_user_id'])
