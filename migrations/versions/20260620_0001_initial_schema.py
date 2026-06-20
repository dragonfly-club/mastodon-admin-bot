"""initial schema

Revision ID: 20260620_0001
Revises:
Create Date: 2026-06-20 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260620_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("action_type", sa.String(length=128), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.String(length=128), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("mastodon_username", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "action_locks",
        sa.Column("lock_key", sa.String(length=256), nullable=False),
        sa.Column("action_log_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("lock_key"),
    )
    op.create_table(
        "moderator_links",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("mastodon_account_id", sa.String(length=128), nullable=False),
        sa.Column("mastodon_username", sa.String(length=512), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("telegram_user_id"),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_table(
        "telegram_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "chat_id"),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.String(length=512), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("object_id", sa.String(length=128), nullable=True),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    with op.batch_alter_table("telegram_deliveries") as batch_op:
        batch_op.create_foreign_key(
            "fk_telegram_deliveries_event_id_webhook_events",
            "webhook_events",
            ["event_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("action_logs") as batch_op:
        batch_op.create_foreign_key(
            "fk_action_logs_event_id_webhook_events",
            "webhook_events",
            ["event_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("action_locks") as batch_op:
        batch_op.create_unique_constraint(
            "uq_action_locks_action_log_id",
            ["action_log_id"],
        )
        batch_op.create_foreign_key(
            "fk_action_locks_action_log_id_action_logs",
            "action_logs",
            ["action_log_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("action_locks") as batch_op:
        batch_op.drop_constraint("fk_action_locks_action_log_id_action_logs", type_="foreignkey")
        batch_op.drop_constraint("uq_action_locks_action_log_id", type_="unique")

    with op.batch_alter_table("action_logs") as batch_op:
        batch_op.drop_constraint("fk_action_logs_event_id_webhook_events", type_="foreignkey")

    with op.batch_alter_table("telegram_deliveries") as batch_op:
        batch_op.drop_constraint(
            "fk_telegram_deliveries_event_id_webhook_events",
            type_="foreignkey",
        )

    op.drop_table("webhook_events")
    op.drop_table("telegram_deliveries")
    op.drop_table("oauth_states")
    op.drop_table("moderator_links")
    op.drop_table("action_locks")
    op.drop_table("action_logs")
