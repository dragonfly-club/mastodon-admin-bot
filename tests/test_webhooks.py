import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.mastodon.webhooks import (
    html_to_text,
    is_pending_local_account,
    parse_webhook_payload,
)
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.repository import Repository, create_engine
from mastodon_admin_bot.telegram.keyboards import Action, AdminCallback
from mastodon_admin_bot.telegram.render import admin_account_link
from mastodon_admin_bot.web.routes import (
    _deliver_event_to_chat,
    _object_type_for_event,
    _render_event_message,
    _should_send_new_message,
)


class FakeBot:
    def __init__(self) -> None:
        self.next_message_id = 100
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.edit_error: Exception | None = None
        self.send_started = asyncio.Event()
        self.allow_send = asyncio.Event()

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        self.sent.append((chat_id, text))
        self.send_started.set()
        await self.allow_send.wait()
        message_id = self.next_message_id
        self.next_message_id += 1
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        **_kwargs: Any,
    ) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edited.append((chat_id, message_id, text))


def make_repo(database_url: str) -> tuple[Repository, Any]:
    engine = create_engine(database_url)
    repo = Repository(
        async_sessionmaker(engine, expire_on_commit=False),
        TokenCipher.from_key(Fernet.generate_key().decode()),
    )
    return repo, engine


def test_parse_webhook_payload() -> None:
    event = parse_webhook_payload(
        {"event": "report.created", "created_at": "2024-01-01T00:00:00Z", "object": {"id": "1"}}
    )

    assert event.event == "report.created"
    assert event.object_id == "1"
    assert event.created_at == "2024-01-01T00:00:00Z"


def test_parse_webhook_payload_rejects_missing_object() -> None:
    with pytest.raises(ValueError, match="missing object"):
        parse_webhook_payload({"event": "report.created"})


def test_webhook_object_type_is_limited_to_accounts_and_reports() -> None:
    assert _object_type_for_event("account.created") == "account"
    assert _object_type_for_event("report.created") == "report"
    assert _object_type_for_event("status.created") is None


def test_new_message_policy_waits_for_confirmed_accounts() -> None:
    assert _should_send_new_message("report.created", {"id": "1"})
    assert _should_send_new_message("account.created", {"id": "1", "confirmed": True})
    assert not _should_send_new_message("account.created", {"id": "1", "confirmed": False})
    assert _should_send_new_message("account.updated", {"id": "1", "confirmed": True})
    assert not _should_send_new_message("report.updated", {"id": "1"})


def test_is_pending_local_account() -> None:
    assert is_pending_local_account({"domain": None, "approved": False})
    assert not is_pending_local_account({"domain": "remote.example", "approved": False})
    assert not is_pending_local_account({"domain": None, "approved": True})


def test_html_to_text_strips_markup() -> None:
    assert html_to_text("<p>Hello<br>world</p>") == "Hello world"


def test_admin_account_link_escapes_url_attributes() -> None:
    rendered = admin_account_link(
        {"account": {"acct": "alice", "url": 'https://example.test/?q=" onclick=bad'}}
    )

    assert 'href="https://example.test/?q=&quot; onclick=bad"' in rendered
    assert 'q=" onclick' not in rendered


def test_admin_account_link_rejects_non_http_urls() -> None:
    assert admin_account_link({"account": {"acct": "alice", "url": "javascript:alert(1)"}}) == (
        "<b>@alice</b>"
    )


def test_largest_callback_payload_fits_telegram_limit() -> None:
    callback = AdminCallback(
        action=Action.SUSPEND_TARGET,
        object_id="1234567890123456789",
        target_id="9876543210987654321",
    ).pack()

    assert len(callback.encode()) <= 64


def test_resolved_report_does_not_restore_moderation_keyboard() -> None:
    _text, keyboard = _render_event_message(
        "report.updated",
        {"id": "1", "action_taken": True},
        "https://mastodon.example",
    )

    assert keyboard is None


async def test_concurrent_duplicate_webhook_delivery_sends_once() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    bot = FakeBot()
    locks = KeyedAsyncLocks()
    payload = {
        "id": "1",
        "action_taken": False,
        "account": {"account": {"acct": "reporter"}},
        "target_account": {"id": "2", "account": {"acct": "target"}},
    }

    first = asyncio.create_task(
        _deliver_event_to_chat(
            repository=repo,
            bot=cast(Any, bot),
            webhook_locks=locks,
            chat_id=10,
            object_type="report",
            object_id="1",
            event_name="report.created",
            obj=payload,
            mastodon_origin="https://mastodon.example",
        )
    )
    await bot.send_started.wait()
    second = asyncio.create_task(
        _deliver_event_to_chat(
            repository=repo,
            bot=cast(Any, bot),
            webhook_locks=locks,
            chat_id=10,
            object_type="report",
            object_id="1",
            event_name="report.created",
            obj=payload,
            mastodon_origin="https://mastodon.example",
        )
    )
    bot.allow_send.set()

    assert await first is False
    assert await second is False
    assert len(bot.sent) == 1
    assert [(chat_id, message_id) for chat_id, message_id, _text in bot.edited] == [(10, 100)]
    await engine.dispose()


async def test_duplicate_webhook_noop_edit_is_success() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_message_mapping(
        object_type="report",
        object_id="1",
        chat_id=10,
        message_id=100,
    )
    bot = FakeBot()
    bot.edit_error = TelegramBadRequest(
        method=EditMessageText(text="same"),
        message="Bad Request: message is not modified: specified new message content and "
        "reply markup are exactly the same as a current content and reply markup of the message",
    )

    failed = await _deliver_event_to_chat(
        repository=repo,
        bot=cast(Any, bot),
        webhook_locks=KeyedAsyncLocks(),
        chat_id=10,
        object_type="report",
        object_id="1",
        event_name="report.created",
        obj={
            "id": "1",
            "action_taken": False,
            "account": {"account": {"acct": "reporter"}},
            "target_account": {"id": "2", "account": {"acct": "target"}},
        },
        mastodon_origin="https://mastodon.example",
    )

    assert failed is False
    await engine.dispose()
