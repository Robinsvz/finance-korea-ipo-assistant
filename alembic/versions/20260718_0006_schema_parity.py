"""Finish legacy schema parity and schedule provenance.

Revision ID: 20260718_0006
Revises: 20260718_0005
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "20260718_0006"
down_revision = "20260718_0005"
branch_labels = None
depends_on = None


ADDED_COLUMNS = {
    "ipos": (
        sa.Column("refund_date_source", sa.String(100)),
        sa.Column("refund_date_verified_at", sa.DateTime(timezone=True)),
        sa.Column("payment_date_source", sa.String(100)),
        sa.Column("payment_date_verified_at", sa.DateTime(timezone=True)),
    ),
    "financial_metrics": (sa.Column("revenue_yoy_status", sa.String(30)),),
}

NUMERIC_COLUMNS = {
    "financial_statements": {"amount": sa.Numeric(24, 2)},
    "financial_metrics": {
        "revenue": sa.Numeric(24, 2),
        "gross_profit": sa.Numeric(24, 2),
        "operating_profit": sa.Numeric(24, 2),
        "net_income": sa.Numeric(24, 2),
        "total_assets": sa.Numeric(24, 2),
        "total_liabilities": sa.Numeric(24, 2),
        "total_equity": sa.Numeric(24, 2),
        "operating_cash_flow": sa.Numeric(24, 2),
        "free_cash_flow": sa.Numeric(24, 2),
    },
    "valuation_metrics": {
        "market_cap_at_offer": sa.Numeric(24, 2),
        "enterprise_value_estimate": sa.Numeric(24, 2),
    },
    "ipo_structure_metrics": {"free_float_market_cap": sa.Numeric(24, 2)},
    "listing_prices": {
        "d0_open_price": sa.Numeric(18, 2),
        "d0_high_price": sa.Numeric(18, 2),
        "d0_low_price": sa.Numeric(18, 2),
        "d0_close_price": sa.Numeric(18, 2),
        "d0_turnover_value": sa.Numeric(24, 2),
        "d1_close_price": sa.Numeric(18, 2),
        "d3_close_price": sa.Numeric(18, 2),
    },
    "outcome_evaluations": {
        "offer_price": sa.Numeric(18, 2),
        "estimated_profit_per_share_open": sa.Numeric(18, 2),
        "estimated_profit_per_share_d1": sa.Numeric(18, 2),
        "estimated_profit_per_share_d3": sa.Numeric(18, 2),
    },
}

REQUIRED_INDEXES = {
    "ix_ipos_actual_listing_date": ("ipos", ["actual_listing_date"]),
    "ix_ipos_demand_start_date": ("ipos", ["demand_start_date"]),
    "ix_ipos_demand_end_date": ("ipos", ["demand_end_date"]),
    "ix_score_snapshots_calculated_at": ("score_snapshots", ["calculated_at"]),
}


def upgrade() -> None:
    bind = op.get_bind()
    for table_name, definitions in ADDED_COLUMNS.items():
        existing = {column["name"] for column in inspect(bind).get_columns(table_name)}
        for column in definitions:
            if column.name not in existing:
                op.add_column(table_name, column)

    bind.execute(
        text(
            "UPDATE financial_metrics SET revenue_yoy_status = CASE "
            "WHEN revenue_yoy_growth IS NOT NULL THEN 'calculated' "
            "ELSE COALESCE(revenue_yoy_status, 'unknown_legacy') END"
        )
    )

    _convert_numeric_columns(bind)
    _enforce_required_values(bind)
    _create_missing_indexes(bind)


def _convert_numeric_columns(bind) -> None:
    for table_name, targets in NUMERIC_COLUMNS.items():
        columns = {column["name"]: column for column in inspect(bind).get_columns(table_name)}
        changes = {
            name: (columns[name]["type"], target)
            for name, target in targets.items()
            if name in columns
            and not (
                isinstance(columns[name]["type"], sa.Numeric)
                and not isinstance(columns[name]["type"], sa.Float)
            )
        }
        if not changes:
            continue
        with op.batch_alter_table(table_name, recreate="always") as batch:
            for name, (existing_type, target_type) in changes.items():
                batch.alter_column(name, existing_type=existing_type, type_=target_type)


def _enforce_required_values(bind) -> None:
    targets = {
        "ipos": ("identity_key", "status_changed_at"),
        "score_snapshots": ("calculated_at",),
    }
    bind.execute(
        text(
            "UPDATE ipos SET identity_key = COALESCE(NULLIF(identity_key, ''), 'legacy:' || id), "
            "status_changed_at = COALESCE(status_changed_at, updated_at, CURRENT_TIMESTAMP)"
        )
    )
    bind.execute(
        text(
            "UPDATE score_snapshots SET calculated_at = "
            "COALESCE(calculated_at, updated_at, CURRENT_TIMESTAMP)"
        )
    )
    for table_name, column_names in targets.items():
        columns = {column["name"]: column for column in inspect(bind).get_columns(table_name)}
        changes = [name for name in column_names if columns[name]["nullable"]]
        if not changes:
            continue
        with op.batch_alter_table(table_name, recreate="always") as batch:
            for name in changes:
                batch.alter_column(name, existing_type=columns[name]["type"], nullable=False)


def _create_missing_indexes(bind) -> None:
    for index_name, (table_name, columns) in REQUIRED_INDEXES.items():
        indexes = {index["name"] for index in inspect(bind).get_indexes(table_name)}
        if index_name not in indexes:
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    # The documented rollback path restores the pre-v1 database backup.
    pass
