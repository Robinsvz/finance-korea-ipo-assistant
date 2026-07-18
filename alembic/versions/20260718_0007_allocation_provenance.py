"""Persist allocation assumption update time.

Revision ID: 20260718_0007
Revises: 20260718_0006
Create Date: 2026-07-18
"""

from sqlalchemy import inspect

from alembic import op

revision = "20260718_0007"
down_revision = "20260718_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("allocation_scenarios")
    }
    if "assumption_updated_at" not in columns:
        op.execute("ALTER TABLE allocation_scenarios ADD COLUMN assumption_updated_at DATETIME")


def downgrade() -> None:
    # The documented rollback path restores the pre-v1 database backup.
    pass
