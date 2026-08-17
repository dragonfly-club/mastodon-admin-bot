from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, event, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mastodon_admin_bot.security import TokenCipher

from .models import (
    AppSetting,
    Base,
    BlocklistRule,
    ModerationOperation,
    ModeratorLink,
    OAuthState,
    PendingAccount,
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

    async def claim_oauth_state(
        self,
        state: str,
        *,
        max_age: timedelta = timedelta(minutes=15),
        lease: timedelta = timedelta(minutes=2),
    ) -> int | None:
        now = datetime.now(UTC)
        async with self.sessionmaker() as session:
            oauth_state = await session.get(OAuthState, state)
            if oauth_state is None or oauth_state.consumed_at is not None:
                return None
            if now - _as_utc(oauth_state.created_at) > max_age:
                return None
            stale_claim = now - lease
            result = await session.execute(
                update(OAuthState)
                .where(
                    OAuthState.state == state,
                    OAuthState.consumed_at.is_(None),
                    or_(OAuthState.claimed_at.is_(None), OAuthState.claimed_at < stale_claim),
                )
                .values(claimed_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if cast(CursorResult[Any], result).rowcount != 1:
                return None
            return oauth_state.telegram_user_id

    async def release_oauth_state(self, state: str) -> None:
        async with self.sessionmaker() as session:
            await session.execute(
                update(OAuthState)
                .where(OAuthState.state == state, OAuthState.consumed_at.is_(None))
                .values(claimed_at=None)
            )
            await session.commit()

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

    async def store_moderator_link_and_consume_state(
        self,
        *,
        state: str,
        telegram_user_id: int,
        mastodon_account_id: str,
        mastodon_username: str,
        access_token: str,
        scopes: str,
    ) -> bool:
        encrypted = self.cipher.encrypt(access_token)
        async with self.sessionmaker() as session:
            oauth_state = await session.get(OAuthState, state)
            if (
                oauth_state is None
                or oauth_state.telegram_user_id != telegram_user_id
                or oauth_state.claimed_at is None
                or oauth_state.consumed_at is not None
            ):
                return False
            link = await session.get(ModeratorLink, telegram_user_id)
            if link is None:
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
                link.mastodon_account_id = mastodon_account_id
                link.mastodon_username = mastodon_username
                link.encrypted_access_token = encrypted
                link.scopes = scopes
            oauth_state.consumed_at = datetime.now(UTC)
            await session.commit()
            return True

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

    async def delete_message_mapping(
        self,
        *,
        object_type: str,
        object_id: str,
        chat_id: int,
    ) -> bool:
        async with self.sessionmaker() as session:
            mapping = await session.scalar(
                select(TelegramMessageMapping).where(
                    TelegramMessageMapping.object_type == object_type,
                    TelegramMessageMapping.object_id == object_id,
                    TelegramMessageMapping.chat_id == chat_id,
                )
            )
            if mapping is None:
                return False
            await session.delete(mapping)
            await session.commit()
            return True

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

    async def add_blocklist_rule(
        self,
        *,
        rule_type: str,
        pattern: str,
        created_by: int | None = None,
    ) -> tuple[BlocklistRule, bool]:
        async with self.sessionmaker() as session:
            existing = await session.scalar(
                select(BlocklistRule).where(
                    BlocklistRule.rule_type == rule_type,
                    BlocklistRule.pattern == pattern,
                )
            )
            if existing is not None:
                await session.commit()
                return existing, False
            rule = BlocklistRule(
                rule_type=rule_type,
                pattern=pattern,
                created_by=created_by,
            )
            session.add(rule)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(BlocklistRule).where(
                        BlocklistRule.rule_type == rule_type,
                        BlocklistRule.pattern == pattern,
                    )
                )
                if existing is None:
                    raise
                return cast(BlocklistRule, existing), False
            await session.refresh(rule)
            return rule, True

    async def remove_blocklist_rule(self, *, rule_type: str, pattern: str) -> int:
        async with self.sessionmaker() as session:
            result = await session.execute(
                delete(BlocklistRule).where(
                    BlocklistRule.rule_type == rule_type,
                    BlocklistRule.pattern == pattern,
                )
            )
            await session.commit()
            return cast(CursorResult[Any], result).rowcount

    async def list_blocklist_rules(self, rule_type: str | None = None) -> list[BlocklistRule]:
        async with self.sessionmaker() as session:
            stmt = select(BlocklistRule).order_by(
                BlocklistRule.rule_type, BlocklistRule.created_at
            )
            if rule_type is not None:
                stmt = stmt.where(BlocklistRule.rule_type == rule_type)
            return list(await session.scalars(stmt))

    async def upsert_pending_account(
        self,
        *,
        account_id: str,
        account_snapshot: str,
        matched_rule_type: str | None = None,
        matched_pattern: str | None = None,
        matched_rule_created_by: int | None = None,
    ) -> PendingAccount:
        async with self.sessionmaker() as session:
            existing = await session.scalar(
                select(PendingAccount).where(PendingAccount.account_id == account_id)
            )
            if existing is not None:
                await session.commit()
                return existing
            pending = PendingAccount(
                account_id=account_id,
                account_snapshot=account_snapshot,
                matched_rule_type=matched_rule_type,
                matched_pattern=matched_pattern,
                matched_rule_created_by=matched_rule_created_by,
                state="pending",
            )
            session.add(pending)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(PendingAccount).where(
                        PendingAccount.account_id == account_id
                    )
                )
                if existing is None:
                    raise
                return cast(PendingAccount, existing)
            await session.refresh(pending)
            return pending

    async def get_pending_account(self, account_id: str) -> PendingAccount | None:
        async with self.sessionmaker() as session:
            return cast(
                PendingAccount | None,
                await session.scalar(
                    select(PendingAccount).where(
                        PendingAccount.account_id == account_id
                    )
                ),
            )

    async def list_due_pending_auto_bans(self, cutoff: datetime) -> list[PendingAccount]:
        async with self.sessionmaker() as session:
            return list(
                await session.scalars(
                    select(PendingAccount).where(
                        PendingAccount.state == "pending",
                        PendingAccount.matched_rule_type.is_not(None),
                        PendingAccount.webhook_received_at <= cutoff,
                    ).order_by(PendingAccount.webhook_received_at)
                )
            )

    async def mark_pending_account_handled(
        self,
        *,
        account_id: str,
        state: str,
        handled_by: str | None = None,
    ) -> None:
        async with self.sessionmaker() as session:
            pending = await session.scalar(
                select(PendingAccount).where(PendingAccount.account_id == account_id)
            )
            if pending is None:
                await session.commit()
                return
            pending.state = state
            pending.handled_at = datetime.now(UTC)
            pending.handled_by = handled_by
            await session.commit()

    async def claim_moderation_operation(
        self,
        *,
        operation_key: str,
        action: str,
        object_type: str,
        object_id: str,
        target_id: str | None,
        requested_by: int | None,
        handled_by: str | None,
    ) -> str:
        now = datetime.now(UTC)
        async with self.sessionmaker() as session:
            operation = ModerationOperation(
                operation_key=operation_key,
                action=action,
                object_type=object_type,
                object_id=object_id,
                target_id=target_id,
                requested_by=requested_by,
                handled_by=handled_by,
                status="processing",
            )
            session.add(operation)
            try:
                await session.commit()
                return "claimed"
            except IntegrityError:
                await session.rollback()
            existing = await session.get(ModerationOperation, operation_key)
            if existing is None:
                return "busy"
            if existing.status == "succeeded":
                return "done"
            if existing.status in {"processing", "uncertain"}:
                return "busy"
            if existing.next_attempt_at is not None and _as_utc(existing.next_attempt_at) > now:
                return "busy"
            result = await session.execute(
                update(ModerationOperation)
                .where(
                    ModerationOperation.operation_key == operation_key,
                    ModerationOperation.status == "failed",
                )
                .values(
                    action=action,
                    object_type=object_type,
                    object_id=object_id,
                    target_id=target_id,
                    requested_by=requested_by,
                    handled_by=handled_by,
                    status="processing",
                    attempts=existing.attempts + 1,
                    last_error=None,
                    next_attempt_at=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
            await session.commit()
            return "claimed" if cast(CursorResult[Any], result).rowcount == 1 else "busy"

    async def complete_moderation_operation(
        self,
        operation_key: str,
        *,
        pending_state: str | None = None,
        handled_by: str | None = None,
        operation_status: str = "succeeded",
    ) -> None:
        now = datetime.now(UTC)
        async with self.sessionmaker() as session:
            operation = await session.get(ModerationOperation, operation_key)
            if operation is None:
                raise RuntimeError("moderation operation disappeared")
            operation.status = operation_status
            operation.completed_at = now
            operation.next_attempt_at = None
            operation.last_error = None
            if pending_state is not None:
                pending = await session.scalar(
                    select(PendingAccount).where(PendingAccount.account_id == operation.object_id)
                )
                if pending is not None and pending.state == "pending":
                    pending.state = pending_state
                    pending.handled_at = now
                    pending.handled_by = handled_by
            await session.commit()

    async def fail_moderation_operation(
        self,
        operation_key: str,
        *,
        error: str,
        uncertain: bool = False,
        retry_after: timedelta | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self.sessionmaker() as session:
            operation = await session.get(ModerationOperation, operation_key)
            if operation is None:
                return
            operation.status = "uncertain" if uncertain else "failed"
            operation.last_error = error[:2000]
            operation.next_attempt_at = now + retry_after if retry_after is not None else None
            await session.commit()

    async def get_moderation_operation(
        self, operation_key: str
    ) -> ModerationOperation | None:
        async with self.sessionmaker() as session:
            return await session.get(ModerationOperation, operation_key)

    async def list_uncertain_moderation_operations(
        self, *, older_than: datetime
    ) -> list[ModerationOperation]:
        async with self.sessionmaker() as session:
            return list(
                await session.scalars(
                    select(ModerationOperation)
                    .where(
                        ModerationOperation.status == "uncertain",
                        ModerationOperation.updated_at <= older_than,
                    )
                    .order_by(ModerationOperation.updated_at)
                )
            )

    async def cleanup_expired_data(self, retention: timedelta) -> dict[str, int]:
        cutoff = datetime.now(UTC) - retention
        async with self.sessionmaker() as session:
            handled_ids = list(
                await session.scalars(
                    select(PendingAccount.account_id).where(
                        PendingAccount.handled_at.is_not(None),
                        PendingAccount.handled_at < cutoff,
                    )
                )
            )
            active_pending_ids = select(PendingAccount.account_id).where(
                PendingAccount.state == "pending"
            )
            mappings_result = await session.execute(
                delete(TelegramMessageMapping).where(
                    or_(
                        and_(
                            TelegramMessageMapping.object_type == "report",
                            TelegramMessageMapping.updated_at < cutoff,
                        ),
                        and_(
                            TelegramMessageMapping.object_type == "account",
                            or_(
                                TelegramMessageMapping.object_id.in_(handled_ids),
                                and_(
                                    TelegramMessageMapping.updated_at < cutoff,
                                    TelegramMessageMapping.object_id.not_in(active_pending_ids),
                                ),
                            ),
                        ),
                    )
                )
            )
            pending_result = await session.execute(
                update(PendingAccount)
                .where(
                    PendingAccount.handled_at.is_not(None),
                    PendingAccount.handled_at < cutoff,
                    or_(
                        PendingAccount.account_snapshot != "{}",
                        PendingAccount.matched_rule_type.is_not(None),
                        PendingAccount.matched_pattern.is_not(None),
                        PendingAccount.matched_rule_created_by.is_not(None),
                    ),
                )
                .values(
                    account_snapshot="{}",
                    matched_rule_type=None,
                    matched_pattern=None,
                    matched_rule_created_by=None,
                )
                .execution_options(synchronize_session=False)
            )
            operations_result = await session.execute(
                delete(ModerationOperation).where(
                    ModerationOperation.completed_at.is_not(None),
                    ModerationOperation.completed_at < cutoff,
                )
            )
            await session.commit()
            return {
                "mappings": cast(CursorResult[Any], mappings_result).rowcount,
                "pending_accounts_scrubbed": cast(CursorResult[Any], pending_result).rowcount,
                "operations": cast(CursorResult[Any], operations_result).rowcount,
            }

    async def check_database(self) -> None:
        async with self.sessionmaker() as session:
            await session.execute(select(1))

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self.sessionmaker() as session:
            setting = await session.get(AppSetting, key)
            return setting.value if setting is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.sessionmaker() as session:
            existing = await session.get(AppSetting, key)
            if existing is None:
                session.add(AppSetting(key=key, value=value))
            else:
                existing.value = value
            await session.commit()

    async def get_notify_blocked_users_enabled(self) -> bool:
        return (await self.get_setting("notify_blocked_users")) == "on"

    async def set_notify_blocked_users_enabled(self, enabled: bool) -> None:
        await self.set_setting("notify_blocked_users", "on" if enabled else "off")

    async def get_autoban_timeout_seconds(self, default: int) -> int:
        raw = await self.get_setting("autoban_reject_after_seconds")
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if value >= 60 else default

    async def set_autoban_timeout_seconds(self, seconds: int) -> None:
        await self.set_setting("autoban_reject_after_seconds", str(seconds))

def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
