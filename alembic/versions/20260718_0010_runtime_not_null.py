"""Enforce non-null runtime safety columns on upgraded SQLite databases.

Revision ID: 20260718_0010
Revises: 20260718_0009
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "20260718_0010"
down_revision = "20260718_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    _make_not_null(bind, "score_snapshots", "input_fingerprint", sa.String(64))
    _make_not_null(bind, "notification_events", "created_at", sa.DateTime(timezone=True))
    _make_not_null(bind, "notification_events", "updated_at", sa.DateTime(timezone=True))


def _make_not_null(bind, table_name: str, column_name: str, existing_type) -> None:
    columns = {column["name"]: column for column in inspect(bind).get_columns(table_name)}
    if columns[column_name]["nullable"]:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=existing_type,
                nullable=False,
            )


def downgrade() -> None:
    # Runtime safety columns stay non-null; restore from backup for a full rollback.
    pass
