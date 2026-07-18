"""Preserve score revisions and add runtime safety metadata.

Revision ID: 20260718_0008
Revises: 20260718_0007
Create Date: 2026-07-18
"""

from sqlalchemy import inspect, text

from alembic import op

revision = "20260718_0008"
down_revision = "20260718_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("score_snapshots")}
    if "input_fingerprint" not in columns:
        bind.execute(text("ALTER TABLE score_snapshots ADD COLUMN input_fingerprint VARCHAR(64)"))
    bind.execute(
        text(
            "UPDATE score_snapshots SET input_fingerprint = 'legacy:' || CAST(id AS TEXT) "
            "WHERE input_fingerprint IS NULL OR input_fingerprint = ''"
        )
    )

    notification_columns = {
        column["name"] for column in inspect(bind).get_columns("notification_events")
    }
    for name, ddl in {
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
        "lease_until": "DATETIME",
        "attempt_count": "INTEGER DEFAULT 0 NOT NULL",
    }.items():
        if name not in notification_columns:
            bind.execute(text(f"ALTER TABLE notification_events ADD COLUMN {name} {ddl}"))
    bind.execute(
        text(
            "UPDATE notification_events SET created_at = "
            "COALESCE(created_at, sent_at, CURRENT_TIMESTAMP), "
            "updated_at = COALESCE(updated_at, sent_at, CURRENT_TIMESTAMP), "
            "attempt_count = COALESCE(attempt_count, 0)"
        )
    )
    bind.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_notification_events_lease_until "
            "ON notification_events (lease_until)"
        )
    )

    underwriter_columns = {
        column["name"] for column in inspect(bind).get_columns("underwriter_allocations")
    }
    for name, ddl in {
        "actual_source": "VARCHAR(100)",
        "actual_recorded_at": "DATETIME",
    }.items():
        if name not in underwriter_columns:
            bind.execute(text(f"ALTER TABLE underwriter_allocations ADD COLUMN {name} {ddl}"))
    bind.execute(
        text(
            "UPDATE underwriter_allocations SET actual_source = "
            "COALESCE(actual_source, assumption_source), "
            "actual_recorded_at = COALESCE(actual_recorded_at, assumption_updated_at) "
            "WHERE actual_applicants_count IS NOT NULL "
            "OR actual_proportional_competition IS NOT NULL"
        )
    )

    indexes = {item["name"]: item for item in inspect(bind).get_indexes("score_snapshots")}
    current = indexes.get("uq_score_snapshot_idx")
    desired = ["ipo_id", "score_date", "model_version", "input_fingerprint"]
    if current and current.get("column_names") != desired:
        bind.execute(text("DROP INDEX uq_score_snapshot_idx"))
    bind.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_score_snapshot_idx ON score_snapshots "
            "(ipo_id, score_date, model_version, input_fingerprint)"
        )
    )


def downgrade() -> None:
    # Score revisions are intentionally retained; restore from backup for a full rollback.
    pass
