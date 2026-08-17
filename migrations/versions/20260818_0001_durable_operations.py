"""durable moderation operations and oauth claims

Revision ID: 20260818_0001
Revises: 20260624_0001
Create Date: 2026-08-18 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0001"
down_revision: str | None = "20260624_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("oauth_states") as batch_op:
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("pending_accounts") as batch_op:
        batch_op.alter_column(
            "state",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
    op.create_table(
        "moderation_operations",
        sa.Column("operation_key", sa.String(length=512), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("handled_by", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("operation_key"),
    )
    op.create_index(
        "ix_moderation_operations_status_next_attempt_at",
        "moderation_operations",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_moderation_operations_status_next_attempt_at",
        table_name="moderation_operations",
    )
    op.drop_table("moderation_operations")
    with op.batch_alter_table("pending_accounts") as batch_op:
        batch_op.alter_column(
            "state",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
    with op.batch_alter_table("oauth_states") as batch_op:
        batch_op.drop_column("claimed_at")
