import pytest

from mastodon_admin_bot.mastodon.webhooks import (
    html_to_text,
    is_pending_local_account,
    parse_webhook_payload,
)
from mastodon_admin_bot.telegram.keyboards import Action, AdminCallback
from mastodon_admin_bot.telegram.render import admin_account_link, render_status_event


def test_parse_webhook_payload() -> None:
    event = parse_webhook_payload(
        {"event": "report.created", "created_at": "2024-01-01T00:00:00Z", "object": {"id": "1"}}
    )

    assert event.event == "report.created"
    assert event.object_id == "1"
    assert event.dedupe_key.startswith("sha256:")
    assert event.legacy_dedupe_key == "report.created:1:2024-01-01T00:00:00Z"


def test_dedupe_key_tracks_exact_payload() -> None:
    first = parse_webhook_payload(
        {"event": "report.created", "created_at": "now", "object": {"id": "1", "comment": "a"}}
    )
    second = parse_webhook_payload(
        {"created_at": "now", "object": {"comment": "a", "id": "1"}, "event": "report.created"}
    )
    changed = parse_webhook_payload(
        {"event": "report.created", "created_at": "now", "object": {"id": "1", "comment": "b"}}
    )

    assert first.dedupe_key == second.dedupe_key
    assert first.dedupe_key != changed.dedupe_key
    assert first.legacy_dedupe_key == changed.legacy_dedupe_key


def test_parse_webhook_payload_rejects_missing_object() -> None:
    with pytest.raises(ValueError, match="missing object"):
        parse_webhook_payload({"event": "report.created"})


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


def test_render_status_event_escapes_url() -> None:
    rendered = render_status_event(
        {"content": "<p>Hello</p>", "url": 'https://example.test/?q=<b>"bad"</b>'},
        "status.created",
    )

    assert "https://example.test/?q=&lt;b&gt;&quot;bad&quot;&lt;/b&gt;" in rendered
    assert 'q=<b>"bad"</b>' not in rendered


def test_largest_callback_payload_fits_telegram_limit() -> None:
    callback = AdminCallback(
        action=Action.SUSPEND_TARGET,
        object_id="1234567890123456789",
        event_id=123456789012,
        target_id="9876543210987654321",
    ).pack()

    assert len(callback.encode()) <= 64
