from typing import Any

import regex
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from mastodon_admin_bot.autoban import (
    MAX_PATTERN_LENGTH,
    RULE_TYPE_EMAIL,
    RULE_TYPE_EMAIL_DOMAIN,
    RULE_TYPE_REASON,
    RULE_TYPE_USED_REASON,
    find_match,
    fit_reason_to_pattern_limit,
    record_used_reason,
    record_used_reason_for_account,
    render_auto_reject_at_line,
    render_match_line,
    rule_type_label,
    snapshot_account,
    snapshot_from_json,
    snapshot_to_json,
    used_reason_display,
    used_reason_pattern,
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


def test_used_reason_pattern_is_anchored_and_case_insensitive() -> None:
    pattern = used_reason_pattern("Buy Crypto")

    assert regex.search(pattern, "buy crypto") is not None
    assert regex.search(pattern, "BUY CRYPTO") is not None
    assert regex.search(pattern, "i want to buy crypto") is None
    assert regex.search(pattern, "buy crypto now") is None


def test_used_reason_display_reverses_escaping() -> None:
    for reason in ("Buy Crypto (urgent)", "buy crypto", "a$b c@d"):
        assert used_reason_display(used_reason_pattern(reason)) == reason
    assert used_reason_display("^custom$") == "^custom$"


def test_render_match_line_decodes_used_reason_pattern() -> None:
    line = render_match_line(RULE_TYPE_USED_REASON, used_reason_pattern("buy crypto"))

    assert "Autoban: used reason pattern" in line
    assert "buy crypto" in line
    assert "(?i)^" not in line


def test_snapshot_account_stores_ip_geo_when_present() -> None:
    snapshot = snapshot_account({"email": "a@b"}, ip_geo="Berlin · AS3320 DIRAC")

    assert snapshot["ip_geo"] == "Berlin · AS3320 DIRAC"
    assert "ip_geo" not in snapshot_account({"email": "a@b"})


async def test_find_match_used_reason() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(
        rule_type=RULE_TYPE_USED_REASON, pattern=used_reason_pattern("buy crypto")
    )
    match = await find_match(
        repo, {"invite_request": "BUY CRYPTO", "account": {"acct": "x"}}
    )

    assert match is not None
    assert match.rule_type == RULE_TYPE_USED_REASON
    await engine.dispose()


async def test_find_match_used_reason_ignores_outer_whitespace() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(
        rule_type=RULE_TYPE_USED_REASON, pattern=used_reason_pattern("buy crypto")
    )

    match = await find_match(repo, {"invite_request": "  buy crypto\n"})

    assert match is not None
    assert match.rule_type == RULE_TYPE_USED_REASON
    await engine.dispose()


async def test_find_match_manual_reason_takes_precedence_over_used_reason() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.add_blocklist_rule(
        rule_type=RULE_TYPE_USED_REASON, pattern=used_reason_pattern("buy crypto")
    )
    await repo.add_blocklist_rule(rule_type=RULE_TYPE_REASON, pattern="crypto")
    match = await find_match(
        repo, {"invite_request": "buy crypto", "account": {"acct": "x"}}
    )

    assert match is not None
    assert match.rule_type == RULE_TYPE_REASON
    await engine.dispose()


async def test_record_used_reason_creates_rule_once() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    assert await record_used_reason(repo, "buy crypto", created_by=7) is True
    assert await record_used_reason(repo, "buy crypto", created_by=7) is False
    assert await record_used_reason(repo, "   ", created_by=7) is False

    rules = await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)
    assert len(rules) == 1
    assert rules[0].created_by == 7
    assert rules[0].pattern == used_reason_pattern("buy crypto")
    await engine.dispose()


async def test_record_used_reason_for_account_reads_snapshot() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)
    await repo.upsert_pending_account(
        account_id="9", account_snapshot='{"reason": "buy crypto"}'
    )
    await repo.upsert_pending_account(
        account_id="10", account_snapshot='{"reason": ""}'
    )

    assert await record_used_reason_for_account(repo, "9", created_by=5) is True
    assert await record_used_reason_for_account(repo, "10") is False
    assert await record_used_reason_for_account(repo, "missing") is False
    await engine.dispose()


def test_fit_reason_to_pattern_limit_shortens_long_reasons() -> None:
    assert fit_reason_to_pattern_limit("short reason") == "short reason"

    fitted = fit_reason_to_pattern_limit("y " * 800)
    assert len(used_reason_pattern(fitted)) <= MAX_PATTERN_LENGTH
    assert fitted.startswith("y ")


async def test_record_used_reason_truncates_oversized_reasons() -> None:
    repo, engine = make_repo("sqlite+aiosqlite:///:memory:")
    await repo.create_schema(engine)

    assert await record_used_reason(repo, "x" * 2000, created_by=7) is True
    rules = await repo.list_blocklist_rules(RULE_TYPE_USED_REASON)
    assert len(rules) == 1
    assert len(rules[0].pattern) <= MAX_PATTERN_LENGTH
    truncated = used_reason_display(rules[0].pattern)
    assert truncated == "x" * (len(rules[0].pattern) - 6)

    match = await find_match(
        repo, {"invite_request": truncated, "account": {"acct": "x"}}
    )
    assert match is not None
    assert match.rule_type == RULE_TYPE_USED_REASON
    await engine.dispose()
