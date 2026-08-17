from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from mastodon_admin_bot.mastodon.client import MastodonApiError
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.repository import Repository, create_engine
from mastodon_admin_bot.sweeper import _auto_reject_one, auto_reject_due_accounts

_DEFAULT_REJECT_RESULT: dict[str, Any] = {
    "id": "1",
    "approved": False,
    "email": "spam@evil.example",
    "ip": "192.0.2.1",
    "locale": "en",
    "account": {"acct": "spam"},
}


def make_repo(database_url: str) -> tuple[Repository, Any]:
    engine = create_engine(database_url)
    repo = Repository(
        async_sessionmaker(engine, expire_on_commit=False),
        TokenCipher.from_key(Fernet.generate_key().decode()),
    )
    return repo, engine


class FakeMastodonClient:
    def __init__(
        self,
        *,
        reject_result: dict[str, Any] | None = None,
        reject_error: Exception | None = None,
    ) -> None:
        self.reject_result = reject_result if reject_result is not None else _DEFAULT_REJECT_RESULT
        self.reject_error = reject_error
        self.reject_calls: list[str] = []

    async def __aenter__(self) -> FakeMastodonClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pass

    async def reject_account(self, account_id: str) -> dict[str, Any]:
        self.reject_calls.append(account_id)
        if self.reject_error is not None:
            raise self.reject_error
        return self.reject_result


def _patch_mastodon_client(
    monkeypatch: Any,
    *,
    reject_error: Exception | None = None,
    reject_result: dict[str, Any] | None = None,
    capture_tokens: list[str] | None = None,
    capture_clients: list[FakeMastodonClient] | None = None,
) -> None:
    def factory(_origin: str, *, token: str | None = None) -> FakeMastodonClient:
        if capture_tokens is not None and token is not None:
            capture_tokens.append(token)
        client = FakeMastodonClient(reject_result=reject_result, reject_error=reject_error)
        if capture_clients is not None:
            capture_clients.append(client)
        return client

    monkeypatch.setattr("mastodon_admin_bot.sweeper.MastodonClient", factory)


class FakeBot:
    def __init__(self) -> None:
        self.edited_text: list[dict[str, Any]] = []
        self.edited_markup: list[dict[str, Any]] = []

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edited_text.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        self.edited_markup.append(kwargs)


async def _seed_pending(
    repo: Repository,
    *,
    account_id: str,
    snapshot: str = '{"email":"spam@evil.example","reason":"buy crypto"}',
    auto_reject_at: datetime | None = None,
    matched_rule_created_by: int | None = None,
) -> None:
    await repo.upsert_pending_account(
        account_id=account_id,
        account_snapshot=snapshot,
        matched_rule_type="email",
        matched_pattern="spam@",
        matched_rule_created_by=matched_rule_created_by,
        auto_reject_at=auto_reject_at,
    )


async def test_auto_reject_due_accounts_rejects_and_updates_messages(
    monkeypatch: Any,
) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_moderator_link(
        telegram_user_id=111,
        mastodon_account_id="m1",
        mastodon_username="alice",
        access_token="alice-token",
        scopes="admin:write:accounts",
    )
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _seed_pending(repo, account_id="1", auto_reject_at=past)
    await repo.upsert_message_mapping(
        object_type="account", object_id="1", chat_id=10, message_id=100
    )

    clients: list[FakeMastodonClient] = []
    _patch_mastodon_client(monkeypatch, capture_clients=clients)

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo, bot=cast(Any, bot), mastodon_origin="https://m.example"
    )

    assert processed == 1
    assert clients[0].reject_calls == ["1"]
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "auto_rejected"
    assert refreshed.handled_by is not None
    assert "alice" in refreshed.handled_by
    assert len(bot.edited_text) == 1
    assert "Auto-rejected by bot (alice)" in bot.edited_text[0]["text"]
    markup = bot.edited_text[0]["reply_markup"]
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert texts == ["Block Email", "Block Domain", "Block Reason"]
    await engine.dispose()


async def test_auto_reject_skips_when_no_moderator_token() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _seed_pending(repo, account_id="1", auto_reject_at=past)

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo, bot=cast(Any, bot), mastodon_origin="https://m.example"
    )

    assert processed == 0
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "pending"
    await engine.dispose()


async def test_auto_reject_network_error_leaves_pending_for_retry(
    monkeypatch: Any,
) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_moderator_link(
        telegram_user_id=111,
        mastodon_account_id="m1",
        mastodon_username="alice",
        access_token="alice-token",
        scopes="admin:write:accounts",
    )
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _seed_pending(repo, account_id="1", auto_reject_at=past)

    _patch_mastodon_client(monkeypatch, reject_error=httpx.ConnectError("down"))

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo, bot=cast(Any, bot), mastodon_origin="https://m.example"
    )

    assert processed == 0
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "pending"
    await engine.dispose()


async def test_auto_reject_already_handled_error_marks_auto_rejected(
    monkeypatch: Any,
) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_moderator_link(
        telegram_user_id=111,
        mastodon_account_id="m1",
        mastodon_username="alice",
        access_token="alice-token",
        scopes="admin:write:accounts",
    )
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _seed_pending(repo, account_id="1", auto_reject_at=past)
    await repo.upsert_message_mapping(
        object_type="account", object_id="1", chat_id=10, message_id=100
    )

    _patch_mastodon_client(
        monkeypatch, reject_error=MastodonApiError(422, "already rejected")
    )

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo, bot=cast(Any, bot), mastodon_origin="https://m.example"
    )

    assert processed == 1
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "auto_rejected"
    assert bot.edited_text == []
    assert len(bot.edited_markup) == 1
    await engine.dispose()


async def test_auto_reject_permanent_client_error_marks_rejected_error(
    monkeypatch: Any,
) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_moderator_link(
        telegram_user_id=111,
        mastodon_account_id="m1",
        mastodon_username="alice",
        access_token="alice-token",
        scopes="admin:write:accounts",
    )
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _seed_pending(repo, account_id="1", auto_reject_at=past)

    _patch_mastodon_client(
        monkeypatch, reject_error=MastodonApiError(401, "unauthorized")
    )

    bot = FakeBot()
    due = await repo.list_due_pending_auto_bans(datetime.now(UTC))
    assert len(due) == 1
    handled = await _auto_reject_one(
        pending=due[0],
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
    )

    assert handled is True
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "rejected_error"
    await engine.dispose()


async def test_auto_reject_prefers_rule_creator_token(monkeypatch: Any) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_moderator_link(
        telegram_user_id=111,
        mastodon_account_id="m1",
        mastodon_username="alice",
        access_token="alice-token",
        scopes="admin:write:accounts",
    )
    await repo.upsert_moderator_link(
        telegram_user_id=222,
        mastodon_account_id="m2",
        mastodon_username="bob",
        access_token="bob-token",
        scopes="admin:write:accounts",
    )
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _seed_pending(
        repo, account_id="1", auto_reject_at=past, matched_rule_created_by=222
    )

    tokens: list[str] = []
    _patch_mastodon_client(monkeypatch, capture_tokens=tokens)

    bot = FakeBot()
    await auto_reject_due_accounts(
        repository=repo, bot=cast(Any, bot), mastodon_origin="https://m.example"
    )

    assert tokens == ["bob-token"]
    await engine.dispose()
