from typing import cast

import httpx
from aiogram.enums import ChatType

from mastodon_admin_bot.storage.repository import Repository
from mastodon_admin_bot.telegram.handlers import (
    _action_lock_key,
    _action_object,
    _handled_suffix,
    _is_private_chat,
    _run_action_with_lock_cleanup,
)
from mastodon_admin_bot.telegram.keyboards import Action, AdminCallback


class FakeRepository:
    def __init__(self) -> None:
        self.failed_actions: list[tuple[int, str]] = []

    async def mark_action_failed(self, action_id: int, error: str) -> None:
        self.failed_actions.append((action_id, error))


def test_action_lock_key_is_scoped_to_moderated_object() -> None:
    first = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="123", event_id=1)
    duplicate_event = AdminCallback(action=Action.REJECT_ACCOUNT, object_id="123", event_id=2)
    other_account = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="456", event_id=1)

    assert _action_lock_key(first) == _action_lock_key(duplicate_event)
    assert _action_lock_key(first) != _action_lock_key(other_account)


def test_report_state_lock_key_is_scoped_to_event() -> None:
    first_event = AdminCallback(action=Action.RESOLVE_REPORT, object_id="123", event_id=1)
    duplicate_event = AdminCallback(action=Action.REOPEN_REPORT, object_id="123", event_id=1)
    later_event = AdminCallback(action=Action.REOPEN_REPORT, object_id="123", event_id=2)

    assert _action_lock_key(first_event) == _action_lock_key(duplicate_event)
    assert _action_lock_key(first_event) != _action_lock_key(later_event)


def test_target_account_action_logs_target_object() -> None:
    callback = AdminCallback(
        action=Action.SUSPEND_TARGET,
        object_id="report-1",
        event_id=1,
        target_id="account-1",
    )

    assert _action_object(callback) == ("account", "account-1")


def test_linking_requires_private_chat() -> None:
    assert _is_private_chat(ChatType.PRIVATE)
    assert not _is_private_chat(ChatType.GROUP)
    assert not _is_private_chat(ChatType.SUPERGROUP)


def test_handled_suffix_escapes_mastodon_username() -> None:
    suffix = _handled_suffix('admin"><b>bad</b>', Action.RESOLVE_REPORT)

    assert suffix == "\n\nHandled by admin&quot;&gt;&lt;b&gt;bad&lt;/b&gt;: rr"


async def test_action_http_failure_releases_lock() -> None:
    repo = FakeRepository()

    async def fail() -> None:
        raise httpx.ConnectError("temporary network failure")

    message = await _run_action_with_lock_cleanup(cast(Repository, repo), 123, fail)

    assert message == "Mastodon action failed. Please retry."
    assert repo.failed_actions == [(123, "temporary network failure")]
