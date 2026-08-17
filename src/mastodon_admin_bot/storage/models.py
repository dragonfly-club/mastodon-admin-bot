from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class ModeratorLink(Base):
    __tablename__ = "moderator_links"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mastodon_account_id: Mapped[str] = mapped_column(String(128))
    mastodon_username: Mapped[str] = mapped_column(String(512))
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramMessageMapping(Base):
    __tablename__ = "telegram_message_mappings"
    __table_args__ = (UniqueConstraint("object_type", "object_id", "chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(128))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class BlocklistRule(Base):
    __tablename__ = "blocklist_rules"
    __table_args__ = (UniqueConstraint("rule_type", "pattern"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_type: Mapped[str] = mapped_column(String(32))
    pattern: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PendingAccount(Base):
    __tablename__ = "pending_accounts"
    __table_args__ = (
        UniqueConstraint("account_id"),
        Index("ix_pending_accounts_state_auto_reject_at", "state", "auto_reject_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(128))
    account_snapshot: Mapped[str] = mapped_column(Text)
    webhook_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    matched_rule_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    matched_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_rule_created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    auto_reject_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), default="pending")
    handled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    handled_by: Mapped[str | None] = mapped_column(String(512), nullable=True)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )
