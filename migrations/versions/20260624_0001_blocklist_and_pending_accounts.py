"""blocklist and pending accounts

Revision ID: 20260624_0001
Revises: 20260620_0001
Create Date: 2026-06-24 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260624_0001"
down_revision: str | None = "20260620_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blocklist_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_type", sa.String(length=32), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_type", "pattern"),
    )
    op.create_table(
        "pending_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("account_snapshot", sa.Text(), nullable=False),
        sa.Column("webhook_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matched_rule_type", sa.String(length=32), nullable=True),
        sa.Column("matched_pattern", sa.Text(), nullable=True),
        sa.Column("matched_rule_created_by", sa.BigInteger(), nullable=True),
        sa.Column("auto_reject_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handled_by", sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id"),
    )
    op.create_index(
        "ix_pending_accounts_state_auto_reject_at",
        "pending_accounts",
        ["state", "auto_reject_at"],
    )
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index(
        "ix_pending_accounts_state_auto_reject_at", table_name="pending_accounts"
    )
    op.drop_table("pending_accounts")
    op.drop_table("blocklist_rules")
