import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from mastodon_admin_bot.app_state import (
    AUTOBAN_SWEEPER_TASK_KEY,
    MAINTENANCE_TASK_KEY,
    POLLING_TASK_KEY,
)
from mastodon_admin_bot.autoban import AutobanInfo
from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.ipinfo import IpInfo
from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.mastodon.webhooks import (
    MastodonWebhook,
    html_to_text,
    is_pending_local_account,
    parse_webhook_payload,
)
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.repository import Repository, create_engine
from mastodon_admin_bot.telegram.keyboards import Action, AdminCallback
from mastodon_admin_bot.telegram.render import (
    admin_account_link,
    render_account_event,
    render_report_event,
)
from mastodon_admin_bot.web.routes import (
    _deliver_event_to_chat,
    _lookup_ip_geo,
    _object_type_for_event,
    _render_event_message,
    _send_event_message,
    _should_send_new_message,
    build_routes,
)


class FakeBot:
    def __init__(self) -> None:
        self.next_message_id = 100
        self.sent: list[tuple[int, str]] = []
        self.sent_kwargs: list[dict[str, Any]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.edit_error: Exception | None = None
        self.send_started = asyncio.Event()
        self.allow_send = asyncio.Event()

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        **kwargs: Any,
    ) -> SimpleNamespace:
        self.sent.append((chat_id, text))
        self.sent_kwargs.append(kwargs)
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


def test_new_message_policy_sends_pending_account_created() -> None:
    assert _should_send_new_message("report.created", {"id": "1"})
    assert _should_send_new_message("account.created", {"id": "1", "approved": False})
    assert _should_send_new_message("account.created", {"id": "1", "approved": None})
    assert not _should_send_new_message("account.created", {"id": "1", "approved": True})


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


def test_pending_account_message_omits_admin_noise_fields() -> None:
    rendered = render_account_event(
        "account.created",
        {
            "id": "123",
            "approved": False,
            "created_at": "2024-01-01T00:00:00Z",
            "email": "alice@example.test",
            "ip": "192.0.2.1",
            "locale": "en",
            "account": {"acct": "alice"},
        },
    )

    assert "ID:" not in rendered
    assert "Approved:" not in rendered
    assert "Created:" not in rendered
    assert "Email: alice@example.test" in rendered


def test_report_message_shows_remote_forwarding() -> None:
    rendered = render_report_event(
        {
            "id": "1",
            "action_taken": False,
            "forwarded": True,
            "account": {"account": {"acct": "reporter"}},
            "target_account": {
                "domain": "remote.example",
                "account": {"acct": "target@remote.example"},
            },
        }
    )

    assert "Forwarded to remote: yes" in rendered
    assert "Previous strikes:" not in rendered


def test_report_message_omits_forwarding_for_local_target() -> None:
    rendered = render_report_event(
        {
            "id": "1",
            "action_taken": False,
            "forwarded": True,
            "account": {"account": {"acct": "reporter"}},
            "target_account": {"domain": None, "account": {"acct": "target"}},
        }
    )

    assert "Forwarded to remote:" not in rendered
    assert "Previous strikes:" not in rendered


def test_report_status_link_uses_labeled_anchor() -> None:
    rendered = render_report_event(
        {
            "id": "1",
            "action_taken": False,
            "account": {"account": {"acct": "reporter"}},
            "target_account": {"account": {"acct": "target"}},
            "statuses": [
                {
                    "content": "<p>Reported status</p>",
                    "url": 'https://remote.example/@target/123?q="bad"',
                    "account": {"acct": "target"},
                }
            ],
        }
    )

    assert "Reported status" in rendered
    assert 'href="https://remote.example/@target/123?q=&quot;bad&quot;"' in rendered
    assert ">Link↗</a>" in rendered
    assert '123?q="bad"' not in rendered


def test_largest_callback_payload_fits_telegram_limit() -> None:
    callback = AdminCallback(
        action=Action.SUSPEND_TARGET,
        object_id="1234567890123456789",
        target_id="9876543210987654321",
    ).pack()

    assert len(callback.encode()) <= 64


async def test_cancelled_lock_waiter_does_not_leak_key() -> None:
    locks = KeyedAsyncLocks()
    first = await locks.acquire("key")
    waiter = asyncio.create_task(locks.acquire("key"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await first.release()

    assert locks._locks == {}


async def test_healthz_reports_background_task_failure() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    settings = Settings(
        TELEGRAM_BOT_TOKEN="123:token",
        MASTODON_BASE_URL="https://mastodon.example",
        MASTODON_WEBHOOK_SECRET="secret",
        MASTODON_CLIENT_ID="client",
        MASTODON_CLIENT_SECRET="secret",
        MASTODON_REDIRECT_URI="https://bot.example/oauth/callback",
        TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    app = web.Application()
    app.add_routes(build_routes(settings, repo, cast(Any, FakeBot())))
    blocker = asyncio.Event()
    tasks = [asyncio.create_task(blocker.wait()) for _ in range(3)]
    app[POLLING_TASK_KEY] = tasks[0]
    app[AUTOBAN_SWEEPER_TASK_KEY] = tasks[1]
    app[MAINTENANCE_TASK_KEY] = tasks[2]
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        response = await client.get("/healthz")
        assert response.status == 200
        assert (await response.json())["ok"] is True

        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        response = await client.get("/healthz")
        assert response.status == 503
        body = await response.json()
        assert body["components"]["autoban_sweeper"] is False
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        await engine.dispose()


def test_resolved_created_report_keeps_open_button_only() -> None:
    _text, keyboard = _render_event_message(
        "report.created",
        {"id": "1", "action_taken": True},
        "https://mastodon.example",
    )

    assert keyboard is not None
    assert len(keyboard.inline_keyboard) == 1
    assert len(keyboard.inline_keyboard[0]) == 1
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Open"
    assert button.url == "https://mastodon.example/admin/reports/1"


def test_pending_account_without_match_keeps_approve_reject_open() -> None:
    _text, keyboard = _render_event_message(
        "account.created",
        {"id": "123", "approved": False, "email": "alice@example.test"},
        "https://mastodon.example",
    )

    assert keyboard is not None
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert texts == ["Approve", "Reject", "Open"]


def test_pending_account_with_match_shows_force_approve_reject_now() -> None:
    from datetime import UTC, datetime

    autoban = AutobanInfo(
        matched_rule_type="email",
        matched_pattern=r"^spam@.*$",
        auto_reject_at=datetime(2026, 6, 24, 13, 30, 0, tzinfo=UTC),
    )
    text, keyboard = _render_event_message(
        "account.created",
        {"id": "123", "approved": False, "email": "spam@evil.example"},
        "https://mastodon.example",
        autoban=autoban,
    )

    assert keyboard is not None
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert texts == ["Force Approve", "Reject Now"]
    assert "<b>\U0001f916 Auto-blocked registration</b>" in text
    assert "Autoban: email pattern" in text
    local = datetime(2026, 6, 24, 13, 30, 0, tzinfo=UTC).astimezone()
    assert f"Auto-reject at: {local.strftime('%Y-%m-%d %H:%M:%S %:z')}" in text


def test_pending_account_with_unmatched_autoban_info_keeps_default_keyboard() -> None:
    autoban = AutobanInfo()
    _text, keyboard = _render_event_message(
        "account.created",
        {"id": "123", "approved": False, "email": "alice@example.test"},
        "https://mastodon.example",
        autoban=autoban,
    )

    assert keyboard is not None
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert texts == ["Approve", "Reject", "Open"]


async def test_autoban_matched_account_message_is_sent_silently() -> None:
    bot = FakeBot()
    bot.allow_send.set()
    await _send_event_message(
        bot=cast(Any, bot),
        chat_id=10,
        event_name="account.created",
        obj={"id": "123", "approved": False, "email": "spam@evil.example"},
        mastodon_origin="https://mastodon.example",
        autoban=AutobanInfo(
            matched_rule_type="email", matched_pattern=r"^spam@.*$"
        ),
    )

    assert bot.sent_kwargs[-1]["disable_notification"] is True


async def test_notify_enabled_autoban_account_message_triggers_notification() -> None:
    bot = FakeBot()
    bot.allow_send.set()
    await _send_event_message(
        bot=cast(Any, bot),
        chat_id=10,
        event_name="account.created",
        obj={"id": "123", "approved": False, "email": "spam@evil.example"},
        mastodon_origin="https://mastodon.example",
        autoban=AutobanInfo(
            matched_rule_type="email", matched_pattern=r"^spam@.*$", silent=False
        ),
    )

    assert bot.sent_kwargs[-1]["disable_notification"] is False


async def test_unmatched_account_message_is_sent_with_notification() -> None:
    bot = FakeBot()
    bot.allow_send.set()
    await _send_event_message(
        bot=cast(Any, bot),
        chat_id=10,
        event_name="account.created",
        obj={"id": "124", "approved": False, "email": "alice@example.test"},
        mastodon_origin="https://mastodon.example",
    )

    assert bot.sent_kwargs[-1]["disable_notification"] is False


async def test_redelivered_created_event_for_handled_account_does_not_resend() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_pending_account(
        account_id="123",
        account_snapshot='{"email":"spam@evil.example"}',
        matched_rule_type="email",
        matched_pattern="spam@",
    )
    await repo.mark_pending_account_handled(
        account_id="123", state="auto_rejected", handled_by="auto (mod)"
    )

    bot = FakeBot()
    bot.allow_send.set()
    failed = await _deliver_event_to_chat(
        repository=repo,
        bot=cast(Any, bot),
        webhook_locks=KeyedAsyncLocks(),
        chat_id=10,
        object_type="account",
        object_id="123",
        event_name="account.created",
        obj={"id": "123", "approved": False, "email": "spam@evil.example", "domain": None},
        mastodon_origin="https://mastodon.example",
    )

    assert failed is False
    assert bot.sent == []
    await engine.dispose()


async def test_stale_mapping_edit_not_found_resends_fresh_message() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_message_mapping(
        object_type="account",
        object_id="123",
        chat_id=10,
        message_id=999,
    )

    bot = FakeBot()
    bot.allow_send.set()
    bot.edit_error = TelegramBadRequest(
        method=EditMessageText(text="x"),
        message="Bad Request: message to edit not found",
    )

    failed = await _deliver_event_to_chat(
        repository=repo,
        bot=cast(Any, bot),
        webhook_locks=KeyedAsyncLocks(),
        chat_id=10,
        object_type="account",
        object_id="123",
        event_name="account.created",
        obj={"id": "123", "approved": False, "email": "spam@evil.example", "domain": None},
        mastodon_origin="https://mastodon.example",
    )

    assert failed is False
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 10
    mapping = await repo.get_message_mapping(
        object_type="account", object_id="123", chat_id=10
    )
    assert mapping is not None
    assert mapping.message_id != 999
    await engine.dispose()


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


def make_settings(**overrides: object) -> Settings:
    values: dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": "123:token",
        "MASTODON_BASE_URL": "https://mastodon.example",
        "MASTODON_WEBHOOK_SECRET": "secret",
        "MASTODON_CLIENT_ID": "client",
        "MASTODON_CLIENT_SECRET": "secret",
        "MASTODON_REDIRECT_URI": "https://bot.example/oauth/callback",
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "TELEGRAM_HOME_CHAT_IDS": "10",
    }
    values.update(overrides)
    return Settings(**values)


class FakeIpClient:
    def __init__(self, info: IpInfo) -> None:
        self.info = info
        self.lookups: list[str] = []

    async def lookup(self, ip: str) -> IpInfo:
        self.lookups.append(ip)
        return self.info


def _pending_account_obj(ip: str = "192.0.2.1") -> dict[str, Any]:
    return {
        "id": "123",
        "approved": False,
        "domain": None,
        "email": "alice@example.test",
        "ip": ip,
        "locale": "en",
        "account": {"acct": "alice"},
    }


def test_account_message_titles_have_status_emojis() -> None:
    base = _pending_account_obj()

    assert render_account_event("account.created", base).startswith(
        "<b>\u23f3 New pending registration</b>"
    )
    approved = {**base, "approved": True}
    assert render_account_event("account.approved", approved).startswith(
        "<b>\u2705 Approved account</b>"
    )
    assert render_account_event("account.created", approved).startswith(
        "<b>\u2705 Approved account</b>"
    )
    assert render_account_event("account.rejected", base).startswith(
        "<b>\U0001f6ab Rejected account</b>"
    )
    assert render_account_event(
        "account.rejected", base, auto_rejected=True
    ).startswith("<b>\U0001f916 Auto-rejected account</b>")
    assert render_account_event("account.created", base, auto_banned=True).startswith(
        "<b>\U0001f916 Auto-blocked registration</b>"
    )


def test_report_title_has_emoji() -> None:
    rendered = render_report_event(
        {
            "id": "1",
            "action_taken": False,
            "account": {"account": {"acct": "reporter"}},
            "target_account": {"account": {"acct": "target"}},
        }
    )

    assert "<b>\U0001f6a8 New Mastodon report</b>" in rendered


def test_account_message_shows_ip_geo_in_parentheses() -> None:
    rendered = render_account_event(
        "account.created",
        _pending_account_obj(),
        ip_geo="Berlin, Germany, <DIRAC>",
    )

    assert "IP: 192.0.2.1 (Berlin, Germany, &lt;DIRAC&gt;)\nLocale: en" in rendered
    assert "IP: 192.0.2.1\nLocale: en" in render_account_event(
        "account.created", _pending_account_obj()
    )


def test_account_message_bounds_webhook_fields() -> None:
    rendered = render_account_event(
        "account.created",
        {
            "account": {"acct": "a" * 1000, "url": "https://example.test/" + "x" * 2000},
            "email": "e" * 1000,
            "ip": "1" * 1000,
            "locale": "l" * 1000,
            "invite_request": "r" * 10000,
        },
        ip_geo="g" * 1000,
    )

    assert len(rendered) < 4096
    assert rendered.count("…") == 6
    assert "<a href=" not in rendered


def test_report_message_bounds_webhook_fields() -> None:
    rendered = render_report_event(
        {
            "id": "i" * 1000,
            "account": {"account": {"acct": "a" * 1000}},
            "target_account": {"account": {"acct": "t" * 1000}},
            "category": "c" * 1000,
            "comment": "m" * 10000,
            "rules": [{"text": "r" * 10000}],
            "statuses": [
                {"account": {"acct": "s" * 1000}, "content": "p" * 10000}
                for _ in range(4)
            ],
        }
    )

    assert len(rendered) < 4096
    assert "…" in rendered
    assert "+1 more attached statuses" in rendered


async def test_lookup_ip_geo_only_for_pending_local_accounts() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    client = FakeIpClient(IpInfo(country="Germany", asn_org="DIRAC"))

    pending_event = MastodonWebhook(
        event="account.created",
        created_at=None,
        object=_pending_account_obj(),
    )
    assert await _lookup_ip_geo(repo, client, pending_event) == "Germany, DIRAC"

    approved_event = MastodonWebhook(
        event="account.created",
        created_at=None,
        object={**_pending_account_obj(), "id": "2", "approved": True},
    )
    remote_event = MastodonWebhook(
        event="account.created",
        created_at=None,
        object={**_pending_account_obj(), "id": "3", "domain": "remote.example"},
    )
    report_event = MastodonWebhook(event="report.created", created_at=None, object={"id": "4"})
    for event in (approved_event, remote_event, report_event):
        assert await _lookup_ip_geo(repo, client, event) == ""

    assert client.lookups == ["192.0.2.1"]
    await engine.dispose()


async def test_lookup_ip_geo_disabled_or_missing_client_returns_empty() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    client = FakeIpClient(IpInfo(country="Germany"))
    event = MastodonWebhook(
        event="account.created",
        created_at=None,
        object=_pending_account_obj(),
    )

    assert await _lookup_ip_geo(repo, None, event) == ""
    await repo.set_ip_lookup_enabled(False)
    assert await _lookup_ip_geo(repo, client, event) == ""
    assert client.lookups == []
    await engine.dispose()


async def test_signed_webhook_delivery_shows_ip_geo_and_stores_snapshot() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    settings = make_settings()
    ip_client = FakeIpClient(IpInfo(country="Germany", asn_org="DIRAC"))
    bot = FakeBot()
    bot.allow_send.set()
    app = web.Application()
    app.add_routes(build_routes(settings, repo, cast(Any, bot), ip_client=ip_client))
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        body = json.dumps(
            {
                "event": "account.created",
                "created_at": "2026-08-26T00:00:00Z",
                "object": _pending_account_obj(),
            }
        ).encode()
        secret = settings.mastodon_webhook_secret.get_secret_value()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        response = await client.post(
            "/mastodon/webhook", data=body, headers={"X-Hub-Signature": signature}
        )

        assert response.status == 200
        assert ip_client.lookups == ["192.0.2.1"]
        assert len(bot.sent) == 1
        text = bot.sent[0][1]
        assert "IP: 192.0.2.1 (Germany, DIRAC)" in text
        pending = await repo.get_pending_account("123")
        assert pending is not None
        assert json.loads(pending.account_snapshot)["ip_geo"] == "Germany, DIRAC"
    finally:
        await client.close()
        await engine.dispose()
