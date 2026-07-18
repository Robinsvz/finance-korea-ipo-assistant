"""Persist partial and skipped job result counts.

Revision ID: 20260718_0009
Revises: 20260718_0008
Create Date: 2026-07-18
"""

from sqlalchemy import inspect, text

from alembic import op

revision = "20260718_0009"
down_revision = "20260718_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("job_runs")}
    for name in ("partial_count", "skipped_count"):
        if name not in columns:
            bind.execute(text(f"ALTER TABLE job_runs ADD COLUMN {name} INTEGER DEFAULT 0 NOT NULL"))


def downgrade() -> None:
    # Operational counters are retained; restore from backup for a full rollback.
    pass
