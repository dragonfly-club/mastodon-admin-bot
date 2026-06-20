from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.migrations import upgrade_database
from mastodon_admin_bot.storage.models import (
    ActionLock,
    ActionLog,
    OAuthState,
    TelegramDelivery,
    WebhookEvent,
)
from mastodon_admin_bot.storage.repository import Repository, create_engine


def make_repo(database_url: str) -> tuple[Repository, AsyncEngine]:
    engine = create_engine(database_url)
    repo = Repository(
        async_sessionmaker(engine, expire_on_commit=False),
        TokenCipher.from_key(Fernet.generate_key().decode()),
    )
    return repo, engine


async def test_moderator_token_round_trip() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    await repo.upsert_moderator_link(
        telegram_user_id=123,
        mastodon_account_id="abc",
        mastodon_username="admin",
        access_token="token",
        scopes="admin:write:reports",
    )

    assert await repo.get_moderator_token(123) == ("token", "admin")
    await engine.dispose()


async def test_migrations_create_usable_schema(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    await upgrade_database(database_url)
    repo, engine = make_repo(database_url)

    await repo.upsert_moderator_link(
        telegram_user_id=123,
        mastodon_account_id="abc",
        mastodon_username="admin",
        access_token="token",
        scopes="admin:write:reports",
    )

    assert await repo.get_moderator_token(123) == ("token", "admin")
    await engine.dispose()


async def test_record_webhook_event_dedupes() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    first, first_inserted = await repo.record_webhook_event(
        dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"event": "report.created", "object": {"id": "1"}},
    )
    second, second_inserted = await repo.record_webhook_event(
        dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"event": "report.created", "object": {"id": "1"}},
    )

    assert first_inserted
    assert not second_inserted
    assert first.id == second.id
    await engine.dispose()


async def test_record_webhook_event_legacy_dedupe_requires_exact_payload() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    old, old_inserted = await repo.record_webhook_event(
        dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"event": "report.created", "object": {"id": "1", "comment": "old"}},
    )
    duplicate, duplicate_inserted = await repo.record_webhook_event(
        dedupe_key="sha256:new",
        legacy_dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"object": {"comment": "old", "id": "1"}, "event": "report.created"},
    )
    changed, changed_inserted = await repo.record_webhook_event(
        dedupe_key="sha256:changed",
        legacy_dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"event": "report.created", "object": {"id": "1", "comment": "new"}},
    )

    assert old_inserted
    assert not duplicate_inserted
    assert duplicate.id == old.id
    assert changed_inserted
    assert changed.id != old.id
    await engine.dispose()


async def test_delivery_tracks_each_chat() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    event, _ = await repo.record_webhook_event(
        dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"event": "report.created", "object": {"id": "1"}},
    )
    await repo.ensure_delivery(event.id, 10)
    await repo.ensure_delivery(event.id, 20)
    await repo.mark_delivery_sent(event.id, 10, 100)
    await repo.mark_delivery_failed(event.id, 20, "telegram failed")

    pending = await repo.get_pending_deliveries(event.id)

    assert [delivery.chat_id for delivery in pending] == [20]
    assert pending[0].status == "failed"
    await engine.dispose()


async def test_failed_action_can_be_retried_but_success_locks_conflict() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    event, _ = await repo.record_webhook_event(
        dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"event": "report.created", "object": {"id": "1"}},
    )

    first = await repo.try_create_action(
        event_id=event.id,
        lock_key="account_decision:1:123",
        action_type="ao",
        object_type="account",
        object_id="123",
        telegram_user_id=10,
        mastodon_username="mod",
    )
    assert first is not None

    duplicate = await repo.try_create_action(
        event_id=event.id,
        lock_key="account_decision:1:123",
        action_type="an",
        object_type="account",
        object_id="123",
        telegram_user_id=20,
        mastodon_username="other",
    )
    assert duplicate is None

    await repo.mark_action_failed(first.id, "temporary failure")
    retry = await repo.try_create_action(
        event_id=event.id,
        lock_key="account_decision:1:123",
        action_type="ao",
        object_type="account",
        object_id="123",
        telegram_user_id=10,
        mastodon_username="mod",
    )
    assert retry is not None
    await repo.mark_action_success(retry.id)

    blocked = await repo.try_create_action(
        event_id=event.id,
        lock_key="account_decision:1:123",
        action_type="an",
        object_type="account",
        object_id="123",
        telegram_user_id=20,
        mastodon_username="other",
    )
    assert blocked is None

    async with repo.sessionmaker() as session:
        statuses = list(await session.scalars(select(ActionLog.status).order_by(ActionLog.id)))
    assert statuses == ["failed", "success"]
    await engine.dispose()


async def test_purge_oauth_states_removes_consumed_and_expired_rows() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    now = datetime.now(UTC)

    async with repo.sessionmaker() as session:
        session.add_all(
            [
                OAuthState(state="fresh", telegram_user_id=1, created_at=now),
                OAuthState(
                    state="expired",
                    telegram_user_id=2,
                    created_at=now - timedelta(minutes=30),
                ),
                OAuthState(
                    state="consumed",
                    telegram_user_id=3,
                    created_at=now,
                    consumed_at=now,
                ),
            ]
        )
        await session.commit()

    deleted = await repo.purge_oauth_states(max_age=timedelta(minutes=15))

    async with repo.sessionmaker() as session:
        states = list(await session.scalars(select(OAuthState.state).order_by(OAuthState.state)))
    assert deleted == 2
    assert states == ["fresh"]
    await engine.dispose()


async def test_foreign_keys_cascade_webhook_children() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    event, _ = await repo.record_webhook_event(
        dedupe_key="report.created:1:now",
        event_type="report.created",
        object_id="1",
        payload={"event": "report.created", "object": {"id": "1"}},
    )
    await repo.ensure_delivery(event.id, 10)
    action = await repo.try_create_action(
        event_id=event.id,
        lock_key="report_state:1:1",
        action_type="rr",
        object_type="report",
        object_id="1",
        telegram_user_id=10,
        mastodon_username="mod",
    )
    assert action is not None

    async with repo.sessionmaker() as session:
        stored_event = await session.get(WebhookEvent, event.id)
        assert stored_event is not None
        await session.delete(stored_event)
        await session.commit()

    async with repo.sessionmaker() as session:
        delivery_count = await session.scalar(select(func.count()).select_from(TelegramDelivery))
        action_count = await session.scalar(select(func.count()).select_from(ActionLog))
        lock_count = await session.scalar(select(func.count()).select_from(ActionLock))
    assert delivery_count == 0
    assert action_count == 0
    assert lock_count == 0
    await engine.dispose()
