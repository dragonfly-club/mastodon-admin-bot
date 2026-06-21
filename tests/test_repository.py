from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.migrations import upgrade_database
from mastodon_admin_bot.storage.models import OAuthState, TelegramMessageMapping
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
