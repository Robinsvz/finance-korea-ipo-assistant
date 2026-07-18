"""Add the Naver API total count.

Revision ID: 20260718_0003
Revises: 20260718_0002
Create Date: 2026-07-18
"""

from sqlalchemy import inspect

from alembic import op

revision = "20260718_0003"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("news_metrics")}
    if "api_total_count" not in columns:
        op.execute("ALTER TABLE news_metrics ADD COLUMN api_total_count INTEGER")


def downgrade() -> None:
    pass
