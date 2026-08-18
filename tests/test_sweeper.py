from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from mastodon_admin_bot.autoban import RULE_TYPE_USED_REASON, used_reason_pattern
from mastodon_admin_bot.mastodon.client import MastodonApiError
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.models import ModerationOperation, PendingAccount
from mastodon_admin_bot.storage.repository import Repository, create_engine
from mastodon_admin_bot.sweeper import (
    _auto_reject_one,
    _record_used_reason_for_operation,
    auto_reject_due_accounts,
    reconcile_uncertain_operations,
)

_DEFAULT_TIMEOUT_SECONDS = 3600


def _default_timeout() -> dict[str, Any]:
    return {
        "default_reject_after_seconds": _DEFAULT_TIMEOUT_SECONDS,
        "trusted_telegram_user_ids": {111, 222},
    }


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

    async def get_admin_account(self, _account_id: str) -> dict[str, Any]:
        if self.reject_error is not None:
            raise self.reject_error
        return self.reject_result

    async def get_admin_report(self, _report_id: str) -> dict[str, Any]:
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
        client = FakeMastodonClient(reject_error=reject_error, reject_result=reject_result)
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
    age: timedelta = timedelta(hours=2),
    matched_rule_created_by: int | None = 111,
) -> None:
    async with repo.sessionmaker() as session:
        session.add(
            PendingAccount(
                account_id=account_id,
                account_snapshot=snapshot,
                matched_rule_type="email",
                matched_pattern="spam@",
                matched_rule_created_by=matched_rule_created_by,
                webhook_received_at=datetime.now(UTC) - age,
                state="pending",
            )
        )
        await session.commit()


async def _seed_moderator(repo: Repository) -> None:
    await repo.upsert_moderator_link(
        telegram_user_id=111,
        mastodon_account_id="m1",
        mastodon_username="alice",
        access_token="alice-token",
        scopes="admin:write:accounts",
    )


async def test_auto_reject_due_accounts_rejects_and_updates_messages(
    monkeypatch: Any,
) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    await _seed_pending(
        repo,
        account_id="1",
        snapshot=(
            '{"email":"spam@evil.example","reason":"buy crypto",'
            '"acct":"spam","ip":"192.0.2.1","locale":"en"}'
        ),
    )
    await repo.upsert_message_mapping(
        object_type="account", object_id="1", chat_id=10, message_id=100
    )

    clients: list[FakeMastodonClient] = []
    # The API response is empty: the message must still render from the snapshot.
    _patch_mastodon_client(monkeypatch, capture_clients=clients, reject_result={})

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        **_default_timeout(),
    )

    assert processed == 1
    assert clients[0].reject_calls == ["1"]
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "auto_rejected"
    assert refreshed.handled_by is not None
    assert "alice" in refreshed.handled_by
    assert len(bot.edited_text) == 1
    assert "\U0001f916 Auto-rejected account" in bot.edited_text[0]["text"]
    assert "Auto-rejected by bot (alice)" in bot.edited_text[0]["text"]
    assert "spam@evil.example" in bot.edited_text[0]["text"]
    assert "@spam" in bot.edited_text[0]["text"]
    assert "192.0.2.1" in bot.edited_text[0]["text"]
    assert "unknown" not in bot.edited_text[0]["text"]
    markup = bot.edited_text[0]["reply_markup"]
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert texts == ["Block Email", "Block Domain"]
    rules = await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)
    assert [rule.pattern for rule in rules] == [used_reason_pattern("buy crypto")]
    await engine.dispose()


async def test_auto_reject_skips_when_no_moderator_token() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_pending(repo, account_id="1")

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        **_default_timeout(),
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
    await _seed_moderator(repo)
    await _seed_pending(repo, account_id="1")

    _patch_mastodon_client(monkeypatch, reject_error=httpx.ConnectError("down"))

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        **_default_timeout(),
    )

    assert processed == 0
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "pending"
    await engine.dispose()


async def test_auto_reject_ambiguous_422_remains_pending(
    monkeypatch: Any,
) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    await _seed_pending(repo, account_id="1")
    await repo.upsert_message_mapping(
        object_type="account", object_id="1", chat_id=10, message_id=100
    )

    _patch_mastodon_client(
        monkeypatch, reject_error=MastodonApiError(422, "already rejected")
    )

    bot = FakeBot()
    processed = await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        **_default_timeout(),
    )

    assert processed == 0
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "pending"
    operation = await repo.get_moderation_operation("account_decision:1")
    assert operation is not None
    assert operation.status == "uncertain"
    assert bot.edited_text == []
    assert bot.edited_markup == []
    await engine.dispose()


async def test_auto_reject_does_not_use_untrusted_rule_creator(monkeypatch: Any) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    await _seed_pending(repo, account_id="1", matched_rule_created_by=111)
    tokens: list[str] = []
    _patch_mastodon_client(monkeypatch, capture_tokens=tokens)

    processed = await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, FakeBot()),
        mastodon_origin="https://m.example",
        default_reject_after_seconds=_DEFAULT_TIMEOUT_SECONDS,
        trusted_telegram_user_ids={222},
    )

    assert processed == 0
    assert tokens == []
    await engine.dispose()


async def test_reconciliation_confirms_missing_account_was_rejected(monkeypatch: Any) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    await _seed_pending(repo, account_id="1")
    await repo.upsert_message_mapping(
        object_type="account", object_id="1", chat_id=10, message_id=100
    )
    await repo.claim_moderation_operation(
        operation_key="account_decision:1",
        action="rn",
        object_type="account",
        object_id="1",
        target_id=None,
        requested_by=111,
        handled_by="auto (alice)",
    )
    await repo.fail_moderation_operation(
        "account_decision:1", error="ambiguous", uncertain=True
    )
    async with repo.sessionmaker() as session:
        operation = await session.get(ModerationOperation, "account_decision:1")
        assert operation is not None
        operation.updated_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    _patch_mastodon_client(
        monkeypatch, reject_error=MastodonApiError(404, "Record not found")
    )
    bot = FakeBot()

    reconciled = await reconcile_uncertain_operations(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        trusted_telegram_user_ids={111},
    )

    assert reconciled == 1
    pending = await repo.get_pending_account("1")
    assert pending is not None and pending.state == "auto_rejected"
    assert len(bot.edited_markup) == 1
    rules = await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)
    assert [rule.pattern for rule in rules] == [used_reason_pattern("buy crypto")]
    await engine.dispose()


async def test_auto_reject_permanent_client_error_remains_pending(
    monkeypatch: Any,
) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    await _seed_pending(repo, account_id="1")

    _patch_mastodon_client(monkeypatch, reject_error=MastodonApiError(401, "unauthorized"))

    bot = FakeBot()
    due = await repo.list_due_pending_auto_bans(
        datetime.now(UTC) - timedelta(seconds=_DEFAULT_TIMEOUT_SECONDS)
    )
    assert len(due) == 1
    handled = await _auto_reject_one(
        pending=due[0],
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        trusted_telegram_user_ids={111},
    )

    assert handled is False
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "pending"
    await engine.dispose()


async def test_auto_reject_prefers_rule_creator_token(monkeypatch: Any) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    await repo.upsert_moderator_link(
        telegram_user_id=222,
        mastodon_account_id="m2",
        mastodon_username="bob",
        access_token="bob-token",
        scopes="admin:write:accounts",
    )
    await _seed_pending(repo, account_id="1", matched_rule_created_by=222)

    tokens: list[str] = []
    _patch_mastodon_client(monkeypatch, capture_tokens=tokens)

    bot = FakeBot()
    await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        **_default_timeout(),
    )

    assert tokens == ["bob-token"]
    await engine.dispose()


async def test_stored_timeout_shortening_makes_old_account_due(monkeypatch: Any) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    # Account arrived 2 hours ago.
    await _seed_pending(repo, account_id="1", age=timedelta(hours=2))

    _patch_mastodon_client(monkeypatch)
    bot = FakeBot()

    # With the 12h default timeout the account is not due yet.
    processed = await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        default_reject_after_seconds=43200,
        trusted_telegram_user_ids={111},
    )
    assert processed == 0
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "pending"

    # Shortening the stored timeout to 1h makes the same old account due.
    await repo.set_autoban_timeout_seconds(3600)
    processed = await auto_reject_due_accounts(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        default_reject_after_seconds=43200,
        trusted_telegram_user_ids={111},
    )
    assert processed == 1
    refreshed = await repo.get_pending_account("1")
    assert refreshed is not None
    assert refreshed.state == "auto_rejected"
    await engine.dispose()


async def test_reconciliation_manual_reject_records_used_reason(monkeypatch: Any) -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await _seed_moderator(repo)
    await _seed_pending(repo, account_id="1")
    await repo.upsert_message_mapping(
        object_type="account", object_id="1", chat_id=10, message_id=100
    )
    await repo.claim_moderation_operation(
        operation_key="account_decision:1",
        action="an",
        object_type="account",
        object_id="1",
        target_id=None,
        requested_by=111,
        handled_by="alice",
    )
    await repo.fail_moderation_operation(
        "account_decision:1", error="ambiguous", uncertain=True
    )
    async with repo.sessionmaker() as session:
        operation = await session.get(ModerationOperation, "account_decision:1")
        assert operation is not None
        operation.updated_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    _patch_mastodon_client(
        monkeypatch, reject_error=MastodonApiError(404, "Record not found")
    )
    bot = FakeBot()

    reconciled = await reconcile_uncertain_operations(
        repository=repo,
        bot=cast(Any, bot),
        mastodon_origin="https://m.example",
        trusted_telegram_user_ids={111},
    )

    assert reconciled == 1
    rules = await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)
    assert len(rules) == 1
    assert rules[0].pattern == used_reason_pattern("buy crypto")
    assert rules[0].created_by == 111
    await engine.dispose()


async def test_record_used_reason_for_operation_respects_flag() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_pending_account(
        account_id="1", account_snapshot='{"reason": "buy crypto"}'
    )
    async with repo.sessionmaker() as session:
        session.add(
            ModerationOperation(
                operation_key="account_decision:1",
                action="an",
                object_type="account",
                object_id="1",
                requested_by=111,
                handled_by="alice",
                status="failed",
            )
        )
        await session.commit()
    operation = await repo.get_moderation_operation("account_decision:1")
    assert operation is not None

    await repo.set_record_used_reasons_enabled(False)
    await _record_used_reason_for_operation(repo, operation)
    assert await repo.list_blocklist_rules(RULE_TYPE_USED_REASON) == []

    await repo.set_record_used_reasons_enabled(True)
    await _record_used_reason_for_operation(repo, operation)
    assert len(await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)) == 1
    await engine.dispose()
