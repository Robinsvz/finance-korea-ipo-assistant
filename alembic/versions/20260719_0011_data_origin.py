"""Mark synthetic IPO rows and preserve data origin explicitly.

Revision ID: 20260719_0011
Revises: 20260718_0010
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "20260719_0011"
down_revision = "20260718_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("ipos")}
    if "is_sample" not in columns:
        op.add_column(
            "ipos",
            sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "data_origin" not in columns:
        op.add_column(
            "ipos",
            sa.Column("data_origin", sa.String(50), nullable=False, server_default="unknown"),
        )
    bind.execute(
        text(
            "UPDATE ipos SET data_origin = 'kind' "
            "WHERE kind_biz_no IS NOT NULL AND kind_biz_no <> ''"
        )
    )
    bind.execute(
        text(
            "UPDATE ipos SET is_sample = 1, data_origin = 'synthetic' "
            "WHERE identity_key LIKE 'sample:%' "
            "OR (identity_key = 'legacy:1' AND corp_name = 'ABC테크')"
        )
    )
    indexes = {index["name"] for index in inspect(bind).get_indexes("ipos")}
    if "ix_ipos_is_sample" not in indexes:
        op.create_index("ix_ipos_is_sample", "ipos", ["is_sample"])
    if "ix_ipos_data_origin" not in indexes:
        op.create_index("ix_ipos_data_origin", "ipos", ["data_origin"])


def downgrade() -> None:
    # Origin labels are deliberately retained; restore from backup for a full rollback.
    pass
