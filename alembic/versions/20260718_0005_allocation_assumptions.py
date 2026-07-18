"""Add per-broker allocation assumptions and actuals.

Revision ID: 20260718_0005
Revises: 20260718_0004
Create Date: 2026-07-18
"""

from sqlalchemy import inspect, text

from alembic import op

revision = "20260718_0005"
down_revision = "20260718_0004"
branch_labels = None
depends_on = None


COLUMNS = {
    "expected_applicants_count": "INTEGER",
    "proportional_competition_conservative": "FLOAT",
    "proportional_competition_base": "FLOAT",
    "proportional_competition_optimistic": "FLOAT",
    "actual_applicants_count": "INTEGER",
    "actual_proportional_competition": "FLOAT",
    "assumption_source": "VARCHAR(100)",
    "assumption_updated_at": "DATETIME",
}


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in inspect(bind).get_columns("underwriter_allocations")}
    for name, ddl in COLUMNS.items():
        if name not in existing:
            bind.execute(text(f'ALTER TABLE underwriter_allocations ADD COLUMN "{name}" {ddl}'))
    scenario_columns = {
        column["name"] for column in inspect(bind).get_columns("allocation_scenarios")
    }
    if "assumption_source" not in scenario_columns:
        bind.execute(
            text("ALTER TABLE allocation_scenarios ADD COLUMN assumption_source VARCHAR(100)")
        )


def downgrade() -> None:
    pass
