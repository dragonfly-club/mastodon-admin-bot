from typing import Any, cast

import regex
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mastodon_admin_bot.autoban import RULE_TYPE_USED_REASON, used_reason_pattern
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.models import BlocklistRule, PendingAccount
from mastodon_admin_bot.storage.repository import Repository, create_engine
from mastodon_admin_bot.telegram.handlers import (
    _account_result_event,
    _action_label,
    _action_lock_key,
    _action_result_text,
    _format_seconds,
    _handled_suffix,
    _is_private_chat,
    _mark_current_message_handled,
    _open_markup,
    _open_url,
    _pending_state_for_action,
    _post_action_markup,
    _post_decision_reply_markup,
    _record_used_reason_for_rejection,
    _remove_blocklist_command,
    _render_blocklist,
    _render_blocklist_pages,
    _snapshot_has_reason,
)
from mastodon_admin_bot.telegram.keyboards import (
    Action,
    AdminCallback,
    applied_block_actions,
    blocklist_page_keyboard,
    post_rejection_keyboard,
)


def make_repo(database_url: str) -> tuple[Repository, AsyncEngine]:
    engine = create_engine(database_url)
    repo = Repository(
        async_sessionmaker(engine, expire_on_commit=False),
        TokenCipher.from_key(Fernet.generate_key().decode()),
    )
    return repo, engine


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


def test_open_url_uses_callback_object_page() -> None:
    account = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="123")
    report = AdminCallback(action=Action.SUSPEND_TARGET, object_id="456", target_id="789")

    assert _open_url("https://mastodon.example/", account) == (
        "https://mastodon.example/admin/accounts/123"
    )
    assert _open_url("https://mastodon.example/", report) == (
        "https://mastodon.example/admin/reports/456"
    )


def test_account_rejection_has_no_open_button() -> None:
    callback = AdminCallback(action=Action.REJECT_ACCOUNT, object_id="123")

    assert _open_markup("https://mastodon.example", callback) is None


def test_account_approval_keeps_open_button() -> None:
    callback = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="123")

    keyboard = _open_markup("https://mastodon.example", callback)

    assert keyboard is not None
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Open"
    assert button.url == "https://mastodon.example/admin/accounts/123"


def test_action_result_text_uses_mastodon_report_result() -> None:
    text = _action_result_text(
        current_text="old report text",
        callback_data=AdminCallback(action=Action.RESOLVE_REPORT, object_id="456"),
        api_result={
            "id": "456",
            "action_taken": True,
            "account": {"account": {"acct": "reporter"}},
            "target_account": {"account": {"acct": "target"}},
        },
        mastodon_username="mod",
    )

    assert "State: resolved" in text
    assert "old report text" not in text
    assert "Handled by mod: Resolved report" in text


def test_action_result_text_falls_back_for_empty_mastodon_result() -> None:
    text = _action_result_text(
        current_text="old report text",
        callback_data=AdminCallback(action=Action.SUSPEND_TARGET, object_id="456", target_id="789"),
        api_result={},
        mastodon_username="mod",
    )

    assert text == "old report text\n\nHandled by mod: Suspended target account"


def test_action_result_text_shows_returned_account_approval() -> None:
    text = _action_result_text(
        current_text="old account text",
        callback_data=AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="123"),
        api_result={
            "id": "123",
            "approved": True,
            "email": "alice@example.test",
            "ip": "192.0.2.1",
            "locale": "en",
            "account": {"acct": "alice"},
        },
        mastodon_username="mod",
    )

    assert "Approved: yes" in text
    assert "old account text" not in text
    assert "Handled by mod: Approved account" in text


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


def test_action_lock_key_groups_conflicting_decisions() -> None:
    approve = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="1")
    reject = AdminCallback(action=Action.REJECT_ACCOUNT, object_id="1")
    other = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="2")

    assert _action_lock_key(approve) == _action_lock_key(reject)
    assert _action_lock_key(approve) != _action_lock_key(other)


def test_action_lock_key_groups_force_approve_and_reject_now_with_decisions() -> None:
    approve = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="1")
    force = AdminCallback(action=Action.FORCE_APPROVE_ACCOUNT, object_id="1")
    reject_now = AdminCallback(action=Action.REJECT_NOW_ACCOUNT, object_id="1")

    assert _action_lock_key(approve) == _action_lock_key(force)
    assert _action_lock_key(approve) == _action_lock_key(reject_now)


def test_action_lock_key_uses_separate_keys_per_block_button() -> None:
    block_email = AdminCallback(action=Action.BLOCK_EMAIL, object_id="1")
    block_domain = AdminCallback(action=Action.BLOCK_EMAIL_DOMAIN, object_id="1")
    decision = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="1")

    assert _action_lock_key(block_email) != _action_lock_key(block_domain)
    assert _action_lock_key(block_email) != _action_lock_key(decision)
    assert _action_lock_key(block_email) == "block_add:1:be"


def test_pending_state_for_action() -> None:
    assert _pending_state_for_action(Action.APPROVE_ACCOUNT) == "approved"
    assert _pending_state_for_action(Action.FORCE_APPROVE_ACCOUNT) == "force_approved"
    assert _pending_state_for_action(Action.REJECT_ACCOUNT) == "rejected"
    assert _pending_state_for_action(Action.REJECT_NOW_ACCOUNT) == "rejected"


def test_account_result_event_handles_force_and_reject_now() -> None:
    assert _account_result_event(Action.FORCE_APPROVE_ACCOUNT) == "account.approved"
    assert _account_result_event(Action.REJECT_NOW_ACCOUNT) == "account.rejected"


def test_action_label_covers_new_actions() -> None:
    assert _action_label(Action.FORCE_APPROVE_ACCOUNT) == "Force approved account"
    assert _action_label(Action.REJECT_NOW_ACCOUNT) == "Rejected account"
    assert _action_label(Action.BLOCK_EMAIL) == "Added blocklist rule"


def test_open_markup_reject_now_has_no_open_button() -> None:
    callback = AdminCallback(action=Action.REJECT_NOW_ACCOUNT, object_id="123")
    assert _open_markup("https://mastodon.example", callback) is None


def test_post_action_markup_reject_shows_block_buttons() -> None:
    callback = AdminCallback(action=Action.REJECT_ACCOUNT, object_id="123")
    markup = _post_action_markup("https://mastodon.example", callback, include_reason=True)
    assert markup is not None
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert texts == ["Block Email", "Block Domain", "Block Reason"]


def test_post_action_markup_reject_without_reason_omits_reason_button() -> None:
    callback = AdminCallback(action=Action.REJECT_NOW_ACCOUNT, object_id="123")
    markup = _post_action_markup(
        "https://mastodon.example", callback, include_reason=False
    )
    assert markup is not None
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert texts == ["Block Email", "Block Domain"]


def test_post_action_markup_approve_keeps_open_button() -> None:
    callback = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="123")
    markup = _post_action_markup("https://mastodon.example", callback, include_reason=False)
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.text == "Open"
    assert button.url == "https://mastodon.example/admin/accounts/123"


def test_post_action_markup_force_approve_keeps_open_button() -> None:
    callback = AdminCallback(action=Action.FORCE_APPROVE_ACCOUNT, object_id="123")
    markup = _post_action_markup("https://mastodon.example", callback, include_reason=False)
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "Open"


async def test_post_action_markup_reject_hides_applied_block_buttons() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    snapshot = {"email": "a@x.com", "email_domain": "x.com", "reason": "spam"}
    await repo.add_blocklist_rule(rule_type="reason", pattern=_pattern("spam"), created_by=1)
    await repo.add_blocklist_rule(rule_type="email", pattern=_pattern("a@x.com"), created_by=1)

    callback = AdminCallback(action=Action.REJECT_NOW_ACCOUNT, object_id="999")
    markup = _post_action_markup(
        "https://mastodon.example",
        callback,
        include_reason=True,
        exclude=await applied_block_actions(repo, snapshot),
    )

    assert markup is not None
    assert _block_button_texts(markup) == ["Block Domain"]
    await engine.dispose()


def test_snapshot_has_reason_reads_pending_account_snapshot() -> None:
    with_reason = PendingAccount(account_id="1", account_snapshot='{"reason":"hi"}')
    without_reason = PendingAccount(account_id="2", account_snapshot='{"reason":""}')
    missing = PendingAccount(account_id="3", account_snapshot="{}")

    assert _snapshot_has_reason(with_reason) is True
    assert _snapshot_has_reason(without_reason) is False
    assert _snapshot_has_reason(missing) is False
    assert _snapshot_has_reason(None) is False


def test_render_blocklist_groups_by_type() -> None:
    rules = [
        BlocklistRule(rule_type="email", pattern=r"^a@$"),
        BlocklistRule(rule_type="email", pattern=r"^b@$"),
        BlocklistRule(rule_type="reason", pattern="spam"),
    ]
    rendered = _render_blocklist(rules)
    assert "<b>email</b> (2):" in rendered
    assert "<b>reason</b> (1):" in rendered
    assert "email_domain" not in rendered
    assert r"^a@$" in rendered
    assert "spam" in rendered


def test_render_blocklist_uses_code_for_patterns() -> None:
    rendered = _render_blocklist(
        [BlocklistRule(rule_type="email", pattern=r"<script>")]
    )
    assert "<code>&lt;script&gt;</code>" in rendered
    assert "<script>" not in rendered


def test_format_seconds_humanizes() -> None:
    assert _format_seconds(30) == "30s"
    assert _format_seconds(120) == "2m"
    assert _format_seconds(3600) == "1h"
    assert _format_seconds(43200) == "12h"
    assert _format_seconds(5400) == "1h 30m"


def test_handled_suffix_for_force_approve() -> None:
    suffix = _handled_suffix("mod", Action.FORCE_APPROVE_ACCOUNT)
    assert "Force approved account" in suffix


def test_action_result_text_force_approve_renders_approved_account() -> None:
    text = _action_result_text(
        current_text="old account text",
        callback_data=AdminCallback(action=Action.FORCE_APPROVE_ACCOUNT, object_id="123"),
        api_result={
            "id": "123",
            "approved": True,
            "email": "spam@evil.example",
            "ip": "192.0.2.1",
            "locale": "en",
            "account": {"acct": "spam"},
        },
        mastodon_username="mod",
    )
    assert "Approved: yes" in text
    assert "Handled by mod: Force approved account" in text


def _snapshot() -> dict[str, str]:
    return {"id": "999", "email": "a@x.com", "email_domain": "x.com", "reason": "spam"}


def _pattern(value: str) -> str:
    return "^" + regex.escape(value) + "$"


async def test_applied_block_actions_hides_reason_when_used_reason_recorded() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    snapshot = _snapshot()

    assert await applied_block_actions(repo, snapshot) == set()
    await repo.add_blocklist_rule(
        rule_type=RULE_TYPE_USED_REASON, pattern=used_reason_pattern("spam"), created_by=1
    )
    assert await applied_block_actions(repo, snapshot) == {Action.BLOCK_REASON}
    await engine.dispose()


async def test_applied_block_actions_tracks_existing_rules() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    snapshot = _snapshot()

    assert await applied_block_actions(repo, snapshot) == set()

    await repo.add_blocklist_rule(rule_type="reason", pattern=_pattern("spam"), created_by=1)
    assert await applied_block_actions(repo, snapshot) == {Action.BLOCK_REASON}

    await repo.add_blocklist_rule(rule_type="email", pattern=_pattern("a@x.com"), created_by=1)
    assert await applied_block_actions(repo, snapshot) == {Action.BLOCK_REASON, Action.BLOCK_EMAIL}

    # A rule for a different value does not hide this account's button.
    await repo.add_blocklist_rule(
        rule_type="email_domain", pattern=_pattern("other.com"), created_by=1
    )
    assert await applied_block_actions(repo, snapshot) == {Action.BLOCK_REASON, Action.BLOCK_EMAIL}

    await engine.dispose()


def _block_button_texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_block_buttons_stay_hidden_across_sequential_clicks() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    snapshot = _snapshot()

    async def keyboard() -> list[str]:
        return _block_button_texts(
            post_rejection_keyboard(
                "999",
                include_reason=True,
                exclude=await applied_block_actions(repo, snapshot),
            )
        )

    assert await keyboard() == ["Block Email", "Block Domain", "Block Reason"]

    await repo.add_blocklist_rule(rule_type="reason", pattern=_pattern("spam"), created_by=1)
    assert await keyboard() == ["Block Email", "Block Domain"]

    await repo.add_blocklist_rule(rule_type="email", pattern=_pattern("a@x.com"), created_by=1)
    assert await keyboard() == ["Block Domain"]

    await repo.add_blocklist_rule(rule_type="email_domain", pattern=_pattern("x.com"), created_by=1)
    assert await keyboard() == []

    await engine.dispose()



async def test_record_used_reason_for_rejection_records_snapshot_reason() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_pending_account(
        account_id="77",
        account_snapshot='{"reason": "buy crypto", "email": "a@b"}',
    )

    await _record_used_reason_for_rejection(repo, "77", 111)
    rules = await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)
    assert len(rules) == 1
    assert rules[0].created_by == 111

    # Missing pending row and empty reasons are ignored.
    await _record_used_reason_for_rejection(repo, "missing", 111)
    await repo.upsert_pending_account(account_id="78", account_snapshot='{"reason": ""}')
    await _record_used_reason_for_rejection(repo, "78", 111)
    assert len(await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)) == 1
    await engine.dispose()


def test_render_blocklist_includes_used_reasons() -> None:
    rules = [
        BlocklistRule(rule_type="used_reason", pattern="(?i)^buy crypto$"),
        BlocklistRule(rule_type="reason", pattern="spam"),
    ]

    rendered = _render_blocklist(rules)
    assert "<b>used_reason</b> (1):" in rendered
    assert "buy crypto" in rendered
    assert "(?i)^" not in rendered
    assert "<b>reason</b> (1):" in rendered


def test_render_blocklist_pages_stay_within_telegram_limit() -> None:
    rules = [
        BlocklistRule(rule_type="used_reason", pattern=used_reason_pattern(f"reason {i} " * 20))
        for i in range(100)
    ]

    pages = _render_blocklist_pages(rules)

    assert len(pages) > 1
    assert all(len(regex.sub(r"</?(?:b|code)>", "", page)) <= 4096 for page in pages)
    assert sum(page.count("<code>") for page in pages) == 100


def test_blocklist_page_keyboard_has_expected_navigation() -> None:
    first = blocklist_page_keyboard(0, 3)
    middle = blocklist_page_keyboard(1, 3)
    last = blocklist_page_keyboard(2, 3)

    assert first is not None
    assert [button.text for button in first.inline_keyboard[0]] == ["Next"]
    assert middle is not None
    assert [button.text for button in middle.inline_keyboard[0]] == ["Prev", "Next"]
    assert last is not None
    assert [button.text for button in last.inline_keyboard[0]] == ["Prev"]
    assert blocklist_page_keyboard(0, 1) is None


async def test_unblock_used_reason_accepts_displayed_exact_text() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(
        rule_type=RULE_TYPE_USED_REASON,
        pattern=used_reason_pattern("Buy Crypto (urgent)"),
    )

    class FakeMessage:
        async def answer(self, _text: str) -> None:
            return None

    class FakeCommand:
        args = "Buy Crypto (urgent)"

    await _remove_blocklist_command(
        cast(Any, FakeMessage()), cast(Any, FakeCommand()), repo, RULE_TYPE_USED_REASON
    )

    assert await repo.list_blocklist_rules(RULE_TYPE_USED_REASON) == []
    await engine.dispose()


async def test_post_decision_reply_markup_hides_applied_block_buttons() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_pending_account(
        account_id="999",
        account_snapshot='{"email": "a@x.com", "email_domain": "x.com", "reason": "spam"}',
    )
    await repo.add_blocklist_rule(rule_type="reason", pattern=_pattern("spam"), created_by=1)
    pending = await repo.get_pending_account("999")

    reject_now = AdminCallback(action=Action.REJECT_NOW_ACCOUNT, object_id="999")
    markup = await _post_decision_reply_markup(
        repo, "https://mastodon.example", reject_now, pending
    )
    assert markup is not None
    assert _block_button_texts(markup) == ["Block Email", "Block Domain"]

    # Non-reject decisions keep the Open button and ignore applied rules.
    approve = AdminCallback(action=Action.APPROVE_ACCOUNT, object_id="999")
    approve_markup = await _post_decision_reply_markup(
        repo, "https://mastodon.example", approve, pending
    )
    assert approve_markup is not None
    assert approve_markup.inline_keyboard[0][0].text == "Open"
    await engine.dispose()


async def test_post_decision_reply_markup_handles_missing_pending() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    reject = AdminCallback(action=Action.REJECT_ACCOUNT, object_id="missing")
    markup = await _post_decision_reply_markup(
        repo, "https://mastodon.example", reject, None
    )
    assert markup is not None
    assert _block_button_texts(markup) == ["Block Email", "Block Domain"]
    await engine.dispose()
