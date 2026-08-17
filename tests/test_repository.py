from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.migrations import upgrade_database
from mastodon_admin_bot.storage.models import (
    OAuthState,
    PendingAccount,
    TelegramMessageMapping,
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


async def test_message_mapping_round_trip() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    await repo.upsert_message_mapping(
        object_type="report",
        object_id="1",
        chat_id=10,
        message_id=100,
    )
    await repo.upsert_message_mapping(
        object_type="report",
        object_id="1",
        chat_id=20,
        message_id=200,
    )
    mapping = await repo.get_message_mapping(
        object_type="report",
        object_id="1",
        chat_id=10,
    )
    mappings = await repo.get_message_mappings(object_type="report", object_id="1")

    assert mapping is not None
    assert mapping.message_id == 100
    assert [(item.chat_id, item.message_id) for item in mappings] == [(10, 100), (20, 200)]
    await engine.dispose()


async def test_message_mapping_upsert_replaces_message_id() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    await repo.upsert_message_mapping(
        object_type="account",
        object_id="1",
        chat_id=10,
        message_id=100,
    )
    await repo.upsert_message_mapping(
        object_type="account",
        object_id="1",
        chat_id=10,
        message_id=101,
    )

    mapping = await repo.get_message_mapping(object_type="account", object_id="1", chat_id=10)

    assert mapping is not None
    assert mapping.message_id == 101
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


async def test_schema_only_contains_oauth_and_message_mappings() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    async with repo.sessionmaker() as session:
        mappings = list(await session.scalars(select(TelegramMessageMapping)))

    assert mappings == []
    await engine.dispose()


async def test_blocklist_rule_round_trip() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    rule, created = await repo.add_blocklist_rule(
        rule_type="email", pattern=r"^spam@.*$", created_by=111
    )
    assert created is True
    assert rule.rule_type == "email"
    assert rule.pattern == r"^spam@.*$"
    assert rule.created_by == 111

    duplicate, created_again = await repo.add_blocklist_rule(
        rule_type="email", pattern=r"^spam@.*$", created_by=222
    )
    assert created_again is False
    assert duplicate.id == rule.id
    assert duplicate.created_by == 111

    rules = await repo.list_blocklist_rules()
    assert [r.pattern for r in rules] == [r"^spam@.*$"]

    domain, _ = await repo.add_blocklist_rule(rule_type="email_domain", pattern="evil")
    listed = await repo.list_blocklist_rules(rule_type="email_domain")
    assert [r.pattern for r in listed] == ["evil"]
    assert [r.pattern for r in await repo.list_blocklist_rules(rule_type="email")] == [
        r"^spam@.*$"
    ]

    removed = await repo.remove_blocklist_rule(rule_type="email", pattern=r"^spam@.*$")
    assert removed == 1
    assert await repo.list_blocklist_rules(rule_type="email") == []
    assert [r.pattern for r in await repo.list_blocklist_rules()] == ["evil"]
    await engine.dispose()


async def test_pending_account_upsert_does_not_reset_state() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    pending = await repo.upsert_pending_account(
        account_id="42",
        account_snapshot='{"email":"a@b"}',
        matched_rule_type="email",
        matched_pattern="a@b",
    )
    assert pending.state == "pending"

    await repo.mark_pending_account_handled(
        account_id="42", state="rejected", handled_by="mod"
    )
    again = await repo.upsert_pending_account(
        account_id="42",
        account_snapshot='{"email":"a@b"}',
    )
    assert again.state == "rejected"
    assert again.handled_by == "mod"
    await engine.dispose()


async def test_list_due_pending_auto_bans_returns_only_due_matched() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    now = datetime.now(UTC)

    async with repo.sessionmaker() as session:
        session.add_all(
            [
                PendingAccount(
                    account_id="due",
                    account_snapshot="{}",
                    matched_rule_type="email",
                    matched_pattern="x",
                    webhook_received_at=now - timedelta(hours=2),
                    state="pending",
                ),
                PendingAccount(
                    account_id="fresh",
                    account_snapshot="{}",
                    matched_rule_type="email",
                    matched_pattern="x",
                    webhook_received_at=now - timedelta(minutes=1),
                    state="pending",
                ),
                PendingAccount(
                    account_id="already",
                    account_snapshot="{}",
                    matched_rule_type="email",
                    matched_pattern="x",
                    webhook_received_at=now - timedelta(hours=5),
                    state="rejected",
                ),
                PendingAccount(
                    account_id="nomatch",
                    account_snapshot="{}",
                    webhook_received_at=now - timedelta(hours=5),
                    state="pending",
                ),
            ]
        )
        await session.commit()

    due = await repo.list_due_pending_auto_bans(now - timedelta(hours=1))
    assert [p.account_id for p in due] == ["due"]
    await engine.dispose()


async def test_notify_blocked_users_setting_defaults_off_and_round_trips() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    assert await repo.get_notify_blocked_users_enabled() is False
    await repo.set_notify_blocked_users_enabled(True)
    assert await repo.get_notify_blocked_users_enabled() is True
    await repo.set_notify_blocked_users_enabled(False)
    assert await repo.get_notify_blocked_users_enabled() is False
    await engine.dispose()


async def test_delete_message_mapping() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    assert await repo.delete_message_mapping(
        object_type="account", object_id="1", chat_id=10
    ) is False
    await repo.upsert_message_mapping(
        object_type="account", object_id="1", chat_id=10, message_id=100
    )
    assert await repo.delete_message_mapping(
        object_type="account", object_id="1", chat_id=10
    ) is True
    assert (
        await repo.get_message_mapping(
            object_type="account", object_id="1", chat_id=10
        )
        is None
    )
    await engine.dispose()


async def test_app_settings_round_trip() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    assert await repo.get_setting("missing") is None
    assert await repo.get_setting("missing", "fallback") == "fallback"

    await repo.set_setting("autoban_reject_after_seconds", "600")
    assert await repo.get_setting("autoban_reject_after_seconds") == "600"
    await repo.set_setting("autoban_reject_after_seconds", "1200")
    assert await repo.get_setting("autoban_reject_after_seconds") == "1200"
    await engine.dispose()


async def test_autoban_timeout_default_and_override() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    assert await repo.get_autoban_timeout_seconds(default=43200) == 43200
    await repo.set_autoban_timeout_seconds(300)
    assert await repo.get_autoban_timeout_seconds(default=43200) == 300
    await repo.set_setting("autoban_reject_after_seconds", "not-a-number")
    assert await repo.get_autoban_timeout_seconds(default=43200) == 43200
    await engine.dispose()


async def test_get_linked_moderator_token_prefers_requested_then_falls_back() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    assert await repo.get_linked_moderator_token() is None
    assert await repo.get_linked_moderator_token(preferred_telegram_user_id=999) is None

    await repo.upsert_moderator_link(
        telegram_user_id=111,
        mastodon_account_id="a1",
        mastodon_username="alice",
        access_token="alice-token",
        scopes="admin:write:accounts",
    )
    await repo.upsert_moderator_link(
        telegram_user_id=222,
        mastodon_account_id="a2",
        mastodon_username="bob",
        access_token="bob-token",
        scopes="admin:write:accounts",
    )

    preferred = await repo.get_linked_moderator_token(preferred_telegram_user_id=222)
    assert preferred is not None
    token, username = preferred
    assert token == "bob-token"
    assert username == "bob"

    fallback = await repo.get_linked_moderator_token(preferred_telegram_user_id=999)
    assert fallback is not None
    token, username = fallback
    assert token in {"alice-token", "bob-token"}
    assert username in {"alice", "bob"}
    await engine.dispose()


async def test_migrations_create_new_tables(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    await upgrade_database(database_url)
    repo, engine = make_repo(database_url)

    rule, created = await repo.add_blocklist_rule(rule_type="reason", pattern="spam")
    assert created is True
    pending = await repo.upsert_pending_account(
        account_id="9", account_snapshot='{"reason":"spam"}'
    )
    assert pending.state == "pending"
    await repo.set_autoban_timeout_seconds(120)
    assert await repo.get_autoban_timeout_seconds(default=43200) == 120
    await engine.dispose()
