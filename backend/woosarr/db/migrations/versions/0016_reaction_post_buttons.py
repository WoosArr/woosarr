"""reaction posts: link and invite buttons under the post

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-15 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # link_button / invite_button: buttons under the post (off by default). buttons_posted: which
    # ones the posted message has, so the page can show that an update is pending.
    with op.batch_alter_table('reaction_posts') as batch_op:
        batch_op.add_column(sa.Column('link_button', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('invite_button', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('buttons_posted', sa.String(length=32), server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('reaction_posts') as batch_op:
        batch_op.drop_column('buttons_posted')
        batch_op.drop_column('invite_button')
        batch_op.drop_column('link_button')
