from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mastodon_admin_bot.security import TokenCipher

from .models import (
    ActionLock,
    ActionLog,
    Base,
    ModeratorLink,
    OAuthState,
    TelegramDelivery,
    WebhookEvent,
)


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, future=True)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class Repository:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], cipher: TokenCipher) -> None:
        self.sessionmaker = sessionmaker
        self.cipher = cipher

    @classmethod
    def from_engine(cls, engine: AsyncEngine, cipher: TokenCipher) -> Repository:
        return cls(async_sessionmaker(engine, expire_on_commit=False), cipher)

    async def create_schema(self, engine: AsyncEngine) -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def create_oauth_state(self, state: str, telegram_user_id: int) -> None:
        await self.purge_oauth_states()
        async with self.sessionmaker() as session:
            session.add(OAuthState(state=state, telegram_user_id=telegram_user_id))
            await session.commit()

    async def purge_oauth_states(self, max_age: timedelta = timedelta(minutes=15)) -> int:
        cutoff = datetime.now(UTC) - max_age
        async with self.sessionmaker() as session:
            states = list(
                await session.scalars(
                    select(OAuthState).where(
                        or_(
                            OAuthState.consumed_at.is_not(None),
                            OAuthState.created_at < cutoff,
                        )
                    )
                )
            )
            for state in states:
                await session.delete(state)
            await session.commit()
            return len(states)

    async def consume_oauth_state(
        self,
        state: str,
        max_age: timedelta = timedelta(minutes=15),
    ) -> int | None:
        async with self.sessionmaker() as session:
            result = await session.get(OAuthState, state)
            if result is None or result.consumed_at is not None:
                return None
            created_at = result.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - created_at > max_age:
                return None
            result.consumed_at = datetime.now(UTC)
            await session.commit()
            return result.telegram_user_id

    async def upsert_moderator_link(
        self,
        *,
        telegram_user_id: int,
        mastodon_account_id: str,
        mastodon_username: str,
        access_token: str,
        scopes: str,
    ) -> None:
        encrypted = self.cipher.encrypt(access_token)
        async with self.sessionmaker() as session:
            existing = await session.get(ModeratorLink, telegram_user_id)
            if existing is None:
                session.add(
                    ModeratorLink(
                        telegram_user_id=telegram_user_id,
                        mastodon_account_id=mastodon_account_id,
                        mastodon_username=mastodon_username,
                        encrypted_access_token=encrypted,
                        scopes=scopes,
                    )
                )
            else:
                existing.mastodon_account_id = mastodon_account_id
                existing.mastodon_username = mastodon_username
                existing.encrypted_access_token = encrypted
                existing.scopes = scopes
            await session.commit()

    async def get_moderator_token(self, telegram_user_id: int) -> tuple[str, str] | None:
        async with self.sessionmaker() as session:
            link = await session.get(ModeratorLink, telegram_user_id)
            if link is None:
                return None
            return self.cipher.decrypt(link.encrypted_access_token), link.mastodon_username

    async def get_moderator_username(self, telegram_user_id: int) -> str | None:
        async with self.sessionmaker() as session:
            link = await session.get(ModeratorLink, telegram_user_id)
            return link.mastodon_username if link else None

    async def record_webhook_event(
        self,
        *,
        dedupe_key: str,
        event_type: str,
        object_id: str | None,
        payload: dict[str, Any],
        legacy_dedupe_key: str | None = None,
    ) -> tuple[WebhookEvent, bool]:
        raw_payload = _canonical_payload(payload)
        async with self.sessionmaker() as session:
            if legacy_dedupe_key is not None:
                legacy = await session.scalar(
                    select(WebhookEvent).where(WebhookEvent.dedupe_key == legacy_dedupe_key)
                )
                if (
                    legacy is not None
                    and _canonical_payload(json.loads(legacy.raw_payload)) == raw_payload
                ):
                    return legacy, False
            event = WebhookEvent(
                dedupe_key=dedupe_key,
                event_type=event_type,
                object_id=object_id,
                raw_payload=raw_payload,
            )
            session.add(event)
            try:
                await session.commit()
                return event, True
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(WebhookEvent).where(WebhookEvent.dedupe_key == dedupe_key)
                )
                if existing is None:
                    raise
                return existing, False

    async def get_webhook_event(self, event_id: int) -> WebhookEvent | None:
        async with self.sessionmaker() as session:
            return await session.get(WebhookEvent, event_id)

    async def get_pending_deliveries(self, event_id: int) -> list[TelegramDelivery]:
        async with self.sessionmaker() as session:
            result = await session.scalars(
                select(TelegramDelivery).where(
                    TelegramDelivery.event_id == event_id,
                    TelegramDelivery.status != "sent",
                )
            )
            return list(result)

    async def ensure_delivery(self, event_id: int, chat_id: int) -> TelegramDelivery:
        async with self.sessionmaker() as session:
            delivery = TelegramDelivery(event_id=event_id, chat_id=chat_id)
            session.add(delivery)
            try:
                await session.commit()
                return delivery
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(TelegramDelivery).where(
                        TelegramDelivery.event_id == event_id,
                        TelegramDelivery.chat_id == chat_id,
                    )
                )
                if existing is None:
                    raise
                return existing

    async def mark_delivery_sent(self, event_id: int, chat_id: int, message_id: int) -> None:
        async with self.sessionmaker() as session:
            delivery = await session.scalar(
                select(TelegramDelivery).where(
                    TelegramDelivery.event_id == event_id,
                    TelegramDelivery.chat_id == chat_id,
                )
            )
            if delivery is not None:
                delivery.message_id = message_id
                delivery.status = "sent"
                delivery.error = None
                await session.commit()

    async def mark_delivery_failed(self, event_id: int, chat_id: int, error: str) -> None:
        async with self.sessionmaker() as session:
            delivery = await session.scalar(
                select(TelegramDelivery).where(
                    TelegramDelivery.event_id == event_id,
                    TelegramDelivery.chat_id == chat_id,
                )
            )
            if delivery is not None:
                delivery.status = "failed"
                delivery.error = error
                await session.commit()

    async def try_create_action(
        self,
        *,
        event_id: int | None,
        lock_key: str,
        action_type: str,
        object_type: str,
        object_id: str,
        telegram_user_id: int,
        mastodon_username: str,
    ) -> ActionLog | None:
        async with self.sessionmaker() as session:
            action = ActionLog(
                event_id=event_id,
                action_type=action_type,
                object_type=object_type,
                object_id=object_id,
                telegram_user_id=telegram_user_id,
                mastodon_username=mastodon_username,
                status="pending",
            )
            session.add(action)
            await session.flush()
            session.add(ActionLock(lock_key=lock_key, action_log_id=action.id))
            try:
                await session.commit()
                return action
            except IntegrityError:
                await session.rollback()
                return None

    async def mark_action_success(self, action_id: int) -> None:
        async with self.sessionmaker() as session:
            action = await session.get(ActionLog, action_id)
            if action is not None:
                action.status = "success"
                action.error = None
                await session.commit()

    async def mark_action_failed(self, action_id: int, error: str) -> None:
        async with self.sessionmaker() as session:
            action = await session.get(ActionLog, action_id)
            if action is None:
                return
            action.status = "failed"
            action.error = error
            lock = await session.scalar(
                select(ActionLock).where(ActionLock.action_log_id == action_id)
            )
            if lock is not None:
                await session.delete(lock)
            await session.commit()
