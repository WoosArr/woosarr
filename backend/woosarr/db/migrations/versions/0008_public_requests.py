"""public requests: post location

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-18 05:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('media_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('message_id', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('media_requests', schema=None) as batch_op:
        batch_op.drop_column('message_id')
        batch_op.drop_column('channel_id')
