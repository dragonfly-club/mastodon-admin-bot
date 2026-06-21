from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
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
