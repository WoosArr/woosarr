"""media requests: when the status last changed, automatic retries of failed requests

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-29 09:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
import woosarr.db.models


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('media_requests') as batch_op:
        batch_op.add_column(sa.Column('status_at', woosarr.db.models.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column('retries', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('retried_at', woosarr.db.models.UTCDateTime(), nullable=True))
    # Until now only open requests were re-read, so a finished request's updated_at is when its
    # status last changed.
    op.execute("UPDATE media_requests SET status_at = updated_at")


def downgrade() -> None:
    with op.batch_alter_table('media_requests') as batch_op:
        batch_op.drop_column('retried_at')
        batch_op.drop_column('retries')
        batch_op.drop_column('status_at')
