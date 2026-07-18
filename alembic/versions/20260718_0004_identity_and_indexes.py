"""Add stable IPO identity and remaining legacy unique indexes.

Revision ID: 20260718_0004
Revises: 20260718_0003
Create Date: 2026-07-18
"""

from sqlalchemy import inspect, text

from alembic import op

revision = "20260718_0004"
down_revision = "20260718_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("ipos")}
    if "identity_key" not in columns:
        bind.execute(text("ALTER TABLE ipos ADD COLUMN identity_key VARCHAR(320)"))
    bind.execute(
        text(
            "UPDATE ipos SET identity_key = CASE "
            "WHEN kind_biz_no IS NOT NULL AND kind_biz_no != '' THEN 'kind:' || kind_biz_no "
            "ELSE 'legacy:' || CAST(id AS TEXT) END "
            "WHERE identity_key IS NULL OR identity_key = ''"
        )
    )
    for table_name, group_columns in (
        ("valuation_metrics", "ipo_id, as_of_date, source"),
        ("ipo_structure_metrics", "ipo_id, as_of_date, source"),
        ("outcome_evaluations", "ipo_id, score_snapshot_id, evaluation_version"),
    ):
        bind.execute(
            text(
                f"DELETE FROM {table_name} WHERE id NOT IN "
                f"(SELECT MAX(id) FROM {table_name} GROUP BY {group_columns})"
            )
        )
    statements = [
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_ipos_identity_key ON ipos (identity_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_valuation_metric_idx "
        "ON valuation_metrics (ipo_id, as_of_date, source)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_structure_metric_idx "
        "ON ipo_structure_metrics (ipo_id, as_of_date, source)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_outcome_evaluation_idx "
        "ON outcome_evaluations (ipo_id, score_snapshot_id, evaluation_version)",
    ]
    for statement in statements:
        bind.execute(text(statement))


def downgrade() -> None:
    pass
