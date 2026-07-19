"""Add user participation and realized-exit ledger.

Revision ID: 20260719_0012
Revises: 20260719_0011
"""

import sqlalchemy as sa

from alembic import op

revision = "20260719_0012"
down_revision = "20260719_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    ledger_tables = {"user_participations", "participation_exits"}
    if ledger_tables.issubset(tables):
        return
    if tables & ledger_tables:
        raise RuntimeError("Participation ledger schema is only partially present")
    op.create_table(
        "user_participations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ipo_id", sa.Integer(), nullable=False),
        sa.Column("score_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("broker_name", sa.String(length=255), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_action", sa.String(length=50), nullable=False),
        sa.Column("user_cash", sa.Integer(), nullable=False),
        sa.Column("applied_shares", sa.Integer(), nullable=False),
        sa.Column("cash_committed", sa.Integer(), nullable=False),
        sa.Column("allocated_shares", sa.Integer(), nullable=True),
        sa.Column("offer_price_snapshot", sa.Numeric(18, 2), nullable=True),
        sa.Column("subscription_fee", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ipo_id"], ["ipos.id"]),
        sa.ForeignKeyConstraint(["score_snapshot_id"], ["score_snapshots.id"]),
        sa.UniqueConstraint("ipo_id", "broker_name", name="uq_user_ipo_broker"),
    )
    op.create_index("ix_user_participations_ipo_id", "user_participations", ["ipo_id"])
    op.create_index(
        "ix_user_participations_score_snapshot_id",
        "user_participations",
        ["score_snapshot_id"],
    )
    op.create_index("ix_user_participations_broker_name", "user_participations", ["broker_name"])
    op.create_index("ix_user_participations_decision_at", "user_participations", ["decision_at"])

    op.create_table(
        "participation_exits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("participation_id", sa.Integer(), nullable=False),
        sa.Column("sold_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy", sa.String(length=50), nullable=False, server_default="MANUAL"),
        sa.Column("sold_shares", sa.Integer(), nullable=False),
        sa.Column("sell_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("selling_cost", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participation_id"], ["user_participations.id"]),
    )
    op.create_index(
        "ix_participation_exits_participation_id",
        "participation_exits",
        ["participation_id"],
    )
    op.create_index("ix_participation_exits_sold_at", "participation_exits", ["sold_at"])


def downgrade() -> None:
    op.drop_index("ix_participation_exits_sold_at", table_name="participation_exits")
    op.drop_index("ix_participation_exits_participation_id", table_name="participation_exits")
    op.drop_table("participation_exits")
    op.drop_index("ix_user_participations_decision_at", table_name="user_participations")
    op.drop_index("ix_user_participations_broker_name", table_name="user_participations")
    op.drop_index("ix_user_participations_score_snapshot_id", table_name="user_participations")
    op.drop_index("ix_user_participations_ipo_id", table_name="user_participations")
    op.drop_table("user_participations")
