"""Add immutable sent-email content audit artifacts.

Revision ID: 20260719_0014
Revises: 20260719_0013
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "20260719_0014"
down_revision = "20260719_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "notification_content_artifacts" in inspect(bind).get_table_names():
        return
    op.create_table(
        "notification_content_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notification_event_id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("subject_sha256", sa.String(length=64), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("html_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "content_redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("universe_hash", sa.String(length=64)),
        sa.Column("evidence_hash", sa.String(length=64)),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["notification_event_id"],
            ["notification_events.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "notification_event_id",
            name="uq_notification_content_artifact_event",
        ),
    )
    op.create_index(
        "ix_notification_content_artifacts_content_sha256",
        "notification_content_artifacts",
        ["content_sha256"],
        unique=False,
    )


def downgrade() -> None:
    if "notification_content_artifacts" in inspect(op.get_bind()).get_table_names():
        op.drop_table("notification_content_artifacts")
