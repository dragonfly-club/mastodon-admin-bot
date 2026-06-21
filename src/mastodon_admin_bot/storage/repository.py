from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

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
    Base,
    ModeratorLink,
    OAuthState,
    TelegramMessageMapping,
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

    async def get_message_mappings(
        self,
        *,
        object_type: str,
        object_id: str,
    ) -> list[TelegramMessageMapping]:
        async with self.sessionmaker() as session:
            mappings = await session.scalars(
                select(TelegramMessageMapping).where(
                    TelegramMessageMapping.object_type == object_type,
                    TelegramMessageMapping.object_id == object_id,
                ).order_by(TelegramMessageMapping.chat_id)
            )
            return list(mappings)

    async def get_message_mapping(
        self,
        *,
        object_type: str,
        object_id: str,
        chat_id: int,
    ) -> TelegramMessageMapping | None:
        async with self.sessionmaker() as session:
            return cast(
                TelegramMessageMapping | None,
                await session.scalar(
                    select(TelegramMessageMapping).where(
                        TelegramMessageMapping.object_type == object_type,
                        TelegramMessageMapping.object_id == object_id,
                        TelegramMessageMapping.chat_id == chat_id,
                    )
                ),
            )

    async def upsert_message_mapping(
        self,
        *,
        object_type: str,
        object_id: str,
        chat_id: int,
        message_id: int,
    ) -> TelegramMessageMapping:
        async with self.sessionmaker() as session:
            mapping = TelegramMessageMapping(
                object_type=object_type,
                object_id=object_id,
                chat_id=chat_id,
                message_id=message_id,
            )
            session.add(mapping)
            try:
                await session.commit()
                return mapping
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(TelegramMessageMapping).where(
                        TelegramMessageMapping.object_type == object_type,
                        TelegramMessageMapping.object_id == object_id,
                        TelegramMessageMapping.chat_id == chat_id,
                    )
                )
                if existing is None:
                    raise
                existing.message_id = message_id
                await session.commit()
                return existing
