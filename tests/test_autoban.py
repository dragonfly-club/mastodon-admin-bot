from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from mastodon_admin_bot.autoban import (
    RULE_TYPE_EMAIL,
    RULE_TYPE_EMAIL_DOMAIN,
    RULE_TYPE_REASON,
    find_match,
    render_auto_reject_at_line,
    render_match_line,
    rule_type_label,
    snapshot_account,
    snapshot_from_json,
    snapshot_to_json,
)
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.repository import Repository, create_engine


def make_repo(database_url: str) -> tuple[Repository, Any]:
    engine = create_engine(database_url)
    repo = Repository(
        async_sessionmaker(engine, expire_on_commit=False),
        TokenCipher.from_key(Fernet.generate_key().decode()),
    )
    return repo, engine


def test_snapshot_account_extracts_fields() -> None:
    snapshot = snapshot_account(
        {
            "email": "spam@evil.example",
            "invite_request": "join me",
            "ip": "192.0.2.1",
            "locale": "en",
            "account": {"acct": "spam"},
            "username": "spam",
        }
    )
    assert snapshot == {
        "email": "spam@evil.example",
        "email_domain": "evil.example",
        "reason": "join me",
        "acct": "spam",
        "ip": "192.0.2.1",
        "locale": "en",
    }


def test_snapshot_account_handles_missing_fields() -> None:
    snapshot = snapshot_account({"id": "1"})
    assert snapshot["email"] == ""
    assert snapshot["email_domain"] == ""
    assert snapshot["reason"] == ""


def test_snapshot_json_round_trip() -> None:
    snapshot = snapshot_account({"email": "a@b", "invite_request": "hi"})
    raw = snapshot_to_json(snapshot)
    assert snapshot_from_json(raw) == snapshot


def test_render_match_line_escapes_pattern() -> None:
    line = render_match_line(RULE_TYPE_EMAIL, r"^<b>bad</b>$")
    assert "Autoban: email pattern" in line
    assert "&lt;b&gt;" in line
    assert "<b>bad</b>" not in line


def test_render_auto_reject_at_line_uses_local_timezone() -> None:
    from datetime import UTC, datetime

    source = datetime(2026, 6, 24, 13, 30, 0, tzinfo=UTC)
    line = render_auto_reject_at_line(source)
    local = source.astimezone()
    assert line == f"Auto-reject at: {local.strftime('%Y-%m-%d %H:%M:%S %:z')}"


def test_rule_type_label() -> None:
    assert rule_type_label(RULE_TYPE_EMAIL) == "email"
    assert rule_type_label(RULE_TYPE_EMAIL_DOMAIN) == "email domain"
    assert rule_type_label(RULE_TYPE_REASON) == "reason"
    assert rule_type_label("unknown") == "unknown"


async def test_find_match_email() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_EMAIL, pattern=r"^spam@.*$")
    match = await find_match(
        repo,
        {"email": "spam@evil.example", "account": {"acct": "spam"}},
    )
    assert match is not None
    assert match.rule_type == RULE_TYPE_EMAIL
    await engine.dispose()


async def test_find_match_email_domain() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_EMAIL_DOMAIN, pattern=r"evil\.example$")
    match = await find_match(
        repo, {"email": "someone@evil.example", "account": {"acct": "x"}}
    )
    assert match is not None
    assert match.rule_type == RULE_TYPE_EMAIL_DOMAIN
    await engine.dispose()


async def test_find_match_reason() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_REASON, pattern=r"buy\s+crypto")
    match = await find_match(
        repo, {"invite_request": "please buy crypto now", "account": {"acct": "x"}}
    )
    assert match is not None
    assert match.rule_type == RULE_TYPE_REASON
    await engine.dispose()


async def test_find_match_no_rules_returns_none() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    match = await find_match(
        repo, {"email": "spam@evil.example", "account": {"acct": "x"}}
    )
    assert match is None
    await engine.dispose()


async def test_find_match_skips_invalid_regex() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_EMAIL, pattern=r"(unclosed")
    match = await find_match(
        repo, {"email": "spam@evil.example", "account": {"acct": "x"}}
    )
    assert match is None
    await engine.dispose()


async def test_find_match_email_takes_precedence_over_domain() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_EMAIL_DOMAIN, pattern=r"evil")
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_EMAIL, pattern=r"^spam@")
    match = await find_match(
        repo, {"email": "spam@evil.example", "account": {"acct": "x"}}
    )
    assert match is not None
    assert match.rule_type == RULE_TYPE_EMAIL
    await engine.dispose()


async def test_find_match_skips_empty_field() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_EMAIL, pattern=r".")
    match = await find_match(repo, {"account": {"acct": "x"}})
    assert match is None
    await engine.dispose()


async def test_find_match_times_out_pathological_regex() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_REASON, pattern=r"^(a|aa)+$")

    match = await find_match(repo, {"invite_request": "a" * 2000 + "!"})

    assert match is None
    await engine.dispose()
