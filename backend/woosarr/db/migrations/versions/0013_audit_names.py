"""audit log: names of the ids each entry mentions

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-27 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL for existing entries: services/audit_names.backfill() names them once at startup.
    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.add_column(sa.Column('names', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.drop_column('names')
