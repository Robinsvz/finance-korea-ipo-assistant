"""Create the rules-v2 schema and upgrade legacy SQLite databases.

Revision ID: 20260718_0001
Revises:
Create Date: 2026-07-18
"""

from __future__ import annotations

from sqlalchemy import inspect, text

import app.models  # noqa: F401
from alembic import op
from app.db import Base

revision = "20260718_0001"
down_revision = None
branch_labels = None
depends_on = None


LEGACY_COLUMNS = {
    "ipos": {
        "status_changed_at": "DATETIME",
        "schedule_version": "INTEGER DEFAULT 1 NOT NULL",
        "needs_review": "BOOLEAN DEFAULT 0 NOT NULL",
        "report_filed_date": "DATE",
        "demand_start_date": "DATE",
        "demand_end_date": "DATE",
        "offer_amount_million": "INTEGER",
        "actual_listing_date": "DATE",
    },
    "demand_forecasts": {
        "receipt_no": "VARCHAR(30)",
        "evidence_json": "JSON",
        "parser_version": "VARCHAR(50) DEFAULT 'dart-tables-v1'",
    },
    "financial_metrics": {
        "source": "VARCHAR(100) DEFAULT 'opendart' NOT NULL",
    },
    "valuation_metrics": {
        "as_of_date": "DATE DEFAULT '1970-01-01' NOT NULL",
        "source": "VARCHAR(100) DEFAULT 'manual' NOT NULL",
    },
    "ipo_structure_metrics": {
        "as_of_date": "DATE DEFAULT '1970-01-01' NOT NULL",
        "source": "VARCHAR(100) DEFAULT 'manual' NOT NULL",
    },
    "news_metrics": {
        "source": "VARCHAR(100) DEFAULT 'naver_news' NOT NULL",
    },
    "score_snapshots": {
        "calculated_at": "DATETIME",
        "confidence_score": "FLOAT DEFAULT 0 NOT NULL",
        "rules_hash": "VARCHAR(64)",
    },
    "listing_prices": {
        "d0_trade_date": "DATE",
        "d1_trade_date": "DATE",
        "d3_trade_date": "DATE",
    },
    "outcome_evaluations": {
        "evaluation_version": "VARCHAR(50) DEFAULT 'outcome-v2' NOT NULL",
    },
}


UNIQUE_INDEXES = {
    "uq_underwriter_ipo_broker_idx": (
        "underwriter_allocations",
        "ipo_id, broker_name",
    ),
    "uq_demand_ipo_receipt_idx": ("demand_forecasts", "ipo_id, receipt_no"),
    "uq_financial_statement_idx": (
        "financial_statements",
        "ipo_id, fiscal_year, statement_type, account_code",
    ),
    "uq_financial_metric_idx": ("financial_metrics", "ipo_id, fiscal_year, source"),
    "uq_news_metric_idx": ("news_metrics", "ipo_id, window_start, window_end, source"),
    "uq_search_trend_idx": (
        "search_trends",
        "ipo_id, keyword_group, window_start, window_end, source",
    ),
    "uq_score_snapshot_idx": ("score_snapshots", "ipo_id, score_date, model_version"),
    "uq_listing_price_idx": ("listing_prices", "ipo_id, listing_date, source"),
    "uq_source_snapshot_idx": ("source_snapshots", "source, source_key, raw_text_hash"),
}


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name, definitions in LEGACY_COLUMNS.items():
        if table_name not in tables:
            continue
        existing = {column["name"] for column in inspect(bind).get_columns(table_name)}
        for column_name, ddl in definitions.items():
            if column_name not in existing:
                statement = f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {ddl}'
                bind.execute(text(statement))

    bind.execute(
        text(
            "UPDATE ipos SET status_changed_at = "
            "COALESCE(status_changed_at, updated_at, CURRENT_TIMESTAMP)"
        )
    )
    bind.execute(
        text(
            "UPDATE score_snapshots SET calculated_at = "
            "COALESCE(calculated_at, updated_at, CURRENT_TIMESTAMP)"
        )
    )

    _deduplicate(bind)
    for index_name, (table_name, columns) in UNIQUE_INDEXES.items():
        bind.execute(
            text(f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns})")
        )


def _deduplicate(bind) -> None:
    groups = {
        "underwriter_allocations": "ipo_id, broker_name",
        "financial_statements": "ipo_id, fiscal_year, statement_type, account_code",
        "financial_metrics": "ipo_id, fiscal_year, source",
        "news_metrics": "ipo_id, window_start, window_end, source",
        "search_trends": "ipo_id, keyword_group, window_start, window_end, source",
        "score_snapshots": "ipo_id, score_date, model_version",
        "listing_prices": "ipo_id, listing_date, source",
        "source_snapshots": "source, source_key, raw_text_hash",
    }
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name, columns in groups.items():
        if table_name not in tables:
            continue
        bind.execute(
            text(
                f"DELETE FROM {table_name} WHERE id NOT IN "
                f"(SELECT MAX(id) FROM {table_name} GROUP BY {columns})"
            )
        )


def downgrade() -> None:
    # The documented rollback path restores the pre-migration database backup.
    pass
