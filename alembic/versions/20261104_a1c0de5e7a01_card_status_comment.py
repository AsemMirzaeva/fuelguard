"""card status history comment

Revision ID: a1c0de5e7a01
Revises: f97c9416ee8e
Create Date: 2026-11-04 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1c0de5e7a01'
down_revision = 'f97c9416ee8e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('card_status_history', sa.Column('comment', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('card_status_history', 'comment')
