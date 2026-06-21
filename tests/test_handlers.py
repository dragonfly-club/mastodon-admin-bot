import asyncio
from typing import Any, cast

import httpx
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.telegram.handlers import (
    _action_lock_key,
    _handled_suffix,
    _is_private_chat,
    _mark_current_message_handled,
    _run_action,
    _run_locked_action,
)
from mastodon_admin_bot.telegram.keyboards import Action, AdminCallback


class FakeBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.edited: list[dict[str, Any]] = []

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edited.append(kwargs)
        if self.error is not None:
            raise self.error


def test_linking_requires_private_chat() -> None:
    assert _is_private_chat(ChatType.PRIVATE)
    assert not _is_private_chat(ChatType.GROUP)
    assert not _is_private_chat(ChatType.SUPERGROUP)


def test_handled_suffix_escapes_mastodon_username() -> None:
    suffix = _handled_suffix('admin"><b>bad</b>', Action.RESOLVE_REPORT)

    assert suffix == "\n\nHandled by admin&quot;&gt;&lt;b&gt;bad&lt;/b&gt;: Resolved report"


async def test_action_http_failure_returns_retry_message() -> None:
    async def fail() -> None:
        raise httpx.ConnectError("temporary network failure")

    message = await _run_action(fail)

    assert message == "Mastodon action failed. Please retry."


async def test_mark_current_message_ignores_message_not_modified() -> None:
    bot = FakeBot(
        TelegramBadRequest(
            method=EditMessageText(text="same"),
            message="Bad Request: message is not modified: specified new message content and "
            "reply markup are exactly the same as a current content and reply markup of the "
            "message",
        )
    )

    await _mark_current_message_handled(
        bot=cast(Any, bot),
        chat_id=10,
        message_id=100,
        text="same",
    )

    assert bot.edited[0]["chat_id"] == 10


async def test_mark_current_message_swallows_other_telegram_edit_errors() -> None:
    bot = FakeBot(
        TelegramBadRequest(
            method=EditMessageText(text="updated"),
            message="Bad Request: message to edit not found",
        )
    )

    await _mark_current_message_handled(
        bot=cast(Any, bot),
        chat_id=10,
        message_id=100,
        text="updated",
    )

    assert bot.edited[0]["message_id"] == 100


async def test_action_lock_rejects_concurrent_conflicting_action() -> None:
    locks = KeyedAsyncLocks()
    handled_keys: set[str] = set()
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def slow_action() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()

    first = asyncio.create_task(
        _run_locked_action(locks, handled_keys, "account_decision:1", slow_action)
    )
    await started.wait()
    second = await _run_locked_action(locks, handled_keys, "account_decision:1", slow_action)
    finish.set()

    assert second == "That moderation decision is already being handled."
    assert await first is None
    assert calls == 1


async def test_action_lock_allows_retry_after_failure() -> None:
    locks = KeyedAsyncLocks()
    handled_keys: set[str] = set()
    calls = 0

    async def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary network failure")

    first = await _run_locked_action(locks, handled_keys, "report_state:1", fail_once)
    second = await _run_locked_action(locks, handled_keys, "report_state:1", fail_once)

    assert first == "Mastodon action failed. Please retry."
    assert second is None
    assert calls == 2


async def test_successful_action_lock_blocks_later_duplicate() -> None:
    locks = KeyedAsyncLocks()
    handled_keys: set[str] = set()
    calls = 0

    async def succeed() -> None:
        nonlocal calls
        calls += 1

    first = await _run_locked_action(locks, handled_keys, "report_state:1", succeed)
    second = await _run_locked_action(locks, handled_keys, "report_state:1", succeed)

    assert first is None
    assert second == "That moderation decision was already handled."
    assert calls == 1


def test_action_lock_key_groups_conflicting_decisions() -> None:
    approve = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="1")
    reject = AdminCallback(action=Action.REJECT_ACCOUNT, object_id="1")
    other = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="2")

    assert _action_lock_key(approve) == _action_lock_key(reject)
    assert _action_lock_key(approve) != _action_lock_key(other)
