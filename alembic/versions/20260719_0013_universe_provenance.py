"""Add stable offering identity and monthly-universe provenance.

Revision ID: 20260719_0013
Revises: 20260719_0012
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "20260719_0013"
down_revision = "20260719_0012"
branch_labels = None
depends_on = None

PROVENANCE_TABLES = {
    "source_documents",
    "ipo_fact_observations",
    "ipo_schedule_revisions",
    "monthly_universe_snapshots",
    "broker_term_versions",
    "data_quality_issues",
}


def upgrade() -> None:
    bind = op.get_bind()
    ipo_columns = {column["name"]: column for column in inspect(bind).get_columns("ipos")}
    if "offering_id" not in ipo_columns:
        op.add_column("ipos", sa.Column("offering_id", sa.String(320), nullable=True))
    _backfill_offering_ids(bind)
    offering_column = {column["name"]: column for column in inspect(bind).get_columns("ipos")}[
        "offering_id"
    ]
    if offering_column["nullable"]:
        with op.batch_alter_table("ipos") as batch_op:
            batch_op.alter_column(
                "offering_id",
                existing_type=sa.String(320),
                nullable=False,
            )
    ipo_indexes = {index["name"] for index in inspect(bind).get_indexes("ipos")}
    if "ix_ipos_offering_id" not in ipo_indexes:
        op.create_index("ix_ipos_offering_id", "ipos", ["offering_id"], unique=True)

    existing_tables = set(inspect(bind).get_table_names())
    if PROVENANCE_TABLES.issubset(existing_tables):
        return
    if PROVENANCE_TABLES & existing_tables:
        raise RuntimeError("Universe provenance schema is only partially present")

    _create_source_documents()
    _create_fact_observations()
    _create_schedule_revisions()
    _create_monthly_snapshots()
    _create_broker_terms()
    _create_data_quality_issues()


def _backfill_offering_ids(bind) -> None:
    rows = bind.execute(
        text(
            "SELECT id, identity_key, offering_id, normalized_name, ticker, kind_biz_no, "
            "dart_corp_code, subscription_start_date, report_filed_date, "
            "actual_listing_date, listing_date FROM ipos ORDER BY id"
        )
    ).mappings()
    materialized_rows = list(rows)
    seen = {
        str(row["offering_id"])
        for row in materialized_rows
        if row["offering_id"] and not str(row["offering_id"]).startswith("offering:temp:")
    }
    for row in materialized_rows:
        current = str(row["offering_id"] or "")
        if current and not current.startswith("offering:temp:"):
            continue
        cycle = next(
            (
                str(row[field])
                for field in (
                    "subscription_start_date",
                    "report_filed_date",
                    "actual_listing_date",
                    "listing_date",
                )
                if row[field]
            ),
            "unknown",
        )
        if row["kind_biz_no"]:
            candidate = f"kind:{row['kind_biz_no']}"
        elif row["dart_corp_code"]:
            candidate = f"dart:{row['dart_corp_code']}:equity:{cycle}"
        elif row["ticker"]:
            listing_cycle = row["actual_listing_date"] or row["listing_date"] or "unknown"
            candidate = f"krx:{row['ticker']}:{listing_cycle}"
        elif cycle != "unknown":
            candidate = f"name:{row['normalized_name']}:{cycle}"
        else:
            candidate = f"offering:legacy:{row['id']}"
        if candidate in seen:
            candidate = f"{candidate}:legacy:{row['id']}"
        seen.add(candidate)
        bind.execute(
            text("UPDATE ipos SET offering_id = :offering_id WHERE id = :ipo_id"),
            {"offering_id": candidate, "ipo_id": row["id"]},
        )


def _create_source_documents() -> None:
    op.create_table(
        "source_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ipo_id", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_id", sa.Integer()),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("document_key", sa.String(255), nullable=False),
        sa.Column("document_type", sa.String(100), nullable=False),
        sa.Column("lineage_root_key", sa.String(255), nullable=False),
        sa.Column("supersedes_document_id", sa.Integer()),
        sa.Column("official_url", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_text_hash", sa.String(64)),
        sa.Column("parser_name", sa.String(100)),
        sa.Column("parser_version", sa.String(50)),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ipo_id"], ["ipos.id"]),
        sa.ForeignKeyConstraint(["source_snapshot_id"], ["source_snapshots.id"]),
        sa.ForeignKeyConstraint(["supersedes_document_id"], ["source_documents.id"]),
        sa.UniqueConstraint("source", "document_key", name="uq_source_document_key"),
    )
    _create_indexes(
        "source_documents",
        (
            "ipo_id",
            "source_snapshot_id",
            "source",
            "document_type",
            "lineage_root_key",
            "supersedes_document_id",
            "published_at",
            "raw_text_hash",
        ),
    )


def _create_fact_observations() -> None:
    op.create_table(
        "ipo_fact_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("observation_key", sa.String(64), nullable=False),
        sa.Column("ipo_id", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Integer()),
        sa.Column("field_name", sa.String(120), nullable=False),
        sa.Column("value_json", sa.JSON()),
        sa.Column("unit", sa.String(50)),
        sa.Column("quality_state", sa.String(40), nullable=False),
        sa.Column("knowledge_phase", sa.String(40), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("effective_from", sa.DateTime(timezone=True)),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("parser_name", sa.String(100)),
        sa.Column("parser_version", sa.String(50)),
        sa.Column("raw_text_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ipo_id"], ["ipos.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"]),
    )
    _create_indexes(
        "ipo_fact_observations",
        (
            "ipo_id",
            "source_document_id",
            "field_name",
            "quality_state",
            "knowledge_phase",
            "published_at",
            "observed_at",
            "fetched_at",
            "effective_from",
            "effective_to",
        ),
    )
    op.create_index(
        "ix_ipo_fact_observations_observation_key",
        "ipo_fact_observations",
        ["observation_key"],
        unique=True,
    )


def _create_schedule_revisions() -> None:
    op.create_table(
        "ipo_schedule_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision_key", sa.String(64), nullable=False),
        sa.Column("ipo_id", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Integer()),
        sa.Column("demand_start_date", sa.Date()),
        sa.Column("demand_end_date", sa.Date()),
        sa.Column("subscription_start_date", sa.Date()),
        sa.Column("subscription_end_date", sa.Date()),
        sa.Column("refund_date", sa.Date()),
        sa.Column("payment_date", sa.Date()),
        sa.Column("planned_listing_date", sa.Date()),
        sa.Column("actual_listing_date", sa.Date()),
        sa.Column("event_status", sa.String(50), nullable=False),
        sa.Column("withdrawal_reason", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ipo_id"], ["ipos.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"]),
    )
    _create_indexes(
        "ipo_schedule_revisions",
        (
            "ipo_id",
            "source_document_id",
            "event_status",
            "published_at",
            "observed_at",
            "effective_from",
        ),
    )
    op.create_index(
        "ix_ipo_schedule_revisions_revision_key",
        "ipo_schedule_revisions",
        ["revision_key"],
        unique=True,
    )


def _create_monthly_snapshots() -> None:
    op.create_table(
        "monthly_universe_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("ipo_id", sa.Integer(), nullable=False),
        sa.Column("offering_id_snapshot", sa.String(320), nullable=False),
        sa.Column("cohort", sa.String(40), nullable=False),
        sa.Column("event_badges_json", sa.JSON(), nullable=False),
        sa.Column("inclusion_dates_json", sa.JSON(), nullable=False),
        sa.Column("reconciliation_state", sa.String(40), nullable=False),
        sa.Column("reconciliation_sources_json", sa.JSON()),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ipo_id"], ["ipos.id"]),
        sa.UniqueConstraint(
            "month_start",
            "ipo_id",
            "snapshot_hash",
            name="uq_monthly_universe_snapshot",
        ),
    )
    _create_indexes(
        "monthly_universe_snapshots",
        (
            "month_start",
            "ipo_id",
            "offering_id_snapshot",
            "cohort",
            "reconciliation_state",
            "captured_at",
        ),
    )


def _create_broker_terms() -> None:
    op.create_table(
        "broker_term_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_key", sa.String(64), nullable=False),
        sa.Column("broker_name", sa.String(255), nullable=False),
        sa.Column("source_document_id", sa.Integer()),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("subscription_fee", sa.Integer()),
        sa.Column("account_open_deadline_rule", sa.String(255)),
        sa.Column("minimum_subscription_shares", sa.Integer()),
        sa.Column("equal_allocation_ratio", sa.Float()),
        sa.Column("proportional_allocation_ratio", sa.Float()),
        sa.Column("terms_json", sa.JSON()),
        sa.Column("official_url", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"]),
    )
    _create_indexes(
        "broker_term_versions",
        ("broker_name", "source_document_id", "effective_from", "effective_to"),
    )
    op.create_index(
        "ix_broker_term_versions_version_key",
        "broker_term_versions",
        ["version_key"],
        unique=True,
    )


def _create_data_quality_issues() -> None:
    op.create_table(
        "data_quality_issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(64), nullable=False),
        sa.Column("ipo_id", sa.Integer()),
        sa.Column("source_document_id", sa.Integer()),
        sa.Column("issue_type", sa.String(40), nullable=False),
        sa.Column("field_name", sa.String(120)),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("details_json", sa.JSON()),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_note", sa.Text()),
        sa.ForeignKeyConstraint(["ipo_id"], ["ipos.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"]),
    )
    _create_indexes(
        "data_quality_issues",
        (
            "ipo_id",
            "source_document_id",
            "issue_type",
            "field_name",
            "severity",
            "status",
        ),
    )
    op.create_index(
        "ix_data_quality_issues_issue_key",
        "data_quality_issues",
        ["issue_key"],
        unique=True,
    )


def _create_indexes(table_name: str, columns: tuple[str, ...]) -> None:
    for column in columns:
        op.create_index(f"ix_{table_name}_{column}", table_name, [column])


def downgrade() -> None:
    for table_name in (
        "data_quality_issues",
        "broker_term_versions",
        "monthly_universe_snapshots",
        "ipo_schedule_revisions",
        "ipo_fact_observations",
        "source_documents",
    ):
        op.drop_table(table_name)
    # Stable identity is deliberately retained so historical links never become orphaned.
