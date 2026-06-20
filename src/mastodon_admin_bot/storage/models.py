from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("dedupe_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(512))
    event_type: Mapped[str] = mapped_column(String(128))
    object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deliveries: Mapped[list[TelegramDelivery]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    action_logs: Mapped[list[ActionLog]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


class TelegramDelivery(Base):
    __tablename__ = "telegram_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("webhook_events.id", ondelete="CASCADE"),
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )
    event: Mapped[WebhookEvent] = relationship(back_populates="deliveries")


class ActionLog(Base):
    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("webhook_events.id", ondelete="CASCADE"),
        nullable=True,
    )
    action_type: Mapped[str] = mapped_column(String(128))
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(128))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    mastodon_username: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="success")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event: Mapped[WebhookEvent | None] = relationship(back_populates="action_logs")
    lock: Mapped[ActionLock | None] = relationship(
        back_populates="action_log",
        cascade="all, delete-orphan",
    )


class ActionLock(Base):
    __tablename__ = "action_locks"

    lock_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    action_log_id: Mapped[int] = mapped_column(
        ForeignKey("action_logs.id", ondelete="CASCADE"),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    action_log: Mapped[ActionLog] = relationship(back_populates="lock")
