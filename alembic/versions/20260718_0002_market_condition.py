"""Add market condition snapshots.

Revision ID: 20260718_0002
Revises: 20260718_0001
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "20260718_0002"
down_revision = "20260718_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "market_condition_snapshots" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "market_condition_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("kosdaq_momentum_20d", sa.Float()),
        sa.Column("recent_ipo_success_rate", sa.Float()),
        sa.Column("volatility_20d", sa.Float()),
        sa.Column("source", sa.String(100), nullable=False, server_default="pykrx+outcomes"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("as_of_date", name="uq_market_condition_date"),
    )
    op.create_index(
        "ix_market_condition_snapshots_as_of_date", "market_condition_snapshots", ["as_of_date"]
    )


def downgrade() -> None:
    if "market_condition_snapshots" not in inspect(op.get_bind()).get_table_names():
        return
    op.drop_index(
        "ix_market_condition_snapshots_as_of_date", table_name="market_condition_snapshots"
    )
    op.drop_table("market_condition_snapshots")
