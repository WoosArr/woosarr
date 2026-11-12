"""reaction posts: retired emoji and clearing old reactions on the next publish

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # retired_options: emoji taken out of a published post; the next publish removes the roles
    # members got from them. clear_old_reactions: that publish also removes the reactions.
    with op.batch_alter_table('reaction_posts') as batch_op:
        batch_op.add_column(sa.Column('retired_options', sa.JSON(), server_default='[]', nullable=False))
        batch_op.add_column(sa.Column('clear_old_reactions', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('reaction_posts') as batch_op:
        batch_op.drop_column('clear_old_reactions')
        batch_op.drop_column('retired_options')
