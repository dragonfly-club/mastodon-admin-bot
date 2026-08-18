from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlsplit

from aiogram.utils.markdown import hbold, hcode

from mastodon_admin_bot.mastodon.webhooks import (
    account_acct,
    account_url,
    summarize_status,
)

EMOJI_PENDING = "⏳"
EMOJI_ACCEPTED = "✅"
EMOJI_AUTO_BLOCKED = "🤖"
EMOJI_BLOCKED = "🚫"
EMOJI_REPORT = "🚨"

_ACCOUNT_NAME_LIMIT = 256
_URL_LIMIT = 1024
_EMAIL_LIMIT = 320
_IP_LIMIT = 64
_IP_GEO_LIMIT = 256
_LOCALE_LIMIT = 64
_REASON_LIMIT = 1500
_REPORT_ID_LIMIT = 128
_CATEGORY_LIMIT = 128
_COMMENT_LIMIT = 1200
_RULES_LIMIT = 500


def _bounded(value: Any, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def admin_account_link(admin_account: dict[str, Any] | None) -> str:
    acct = _bounded(account_acct(admin_account), _ACCOUNT_NAME_LIMIT)
    url = account_url(admin_account)
    if url and len(url) <= _URL_LIMIT and _is_safe_http_url(url):
        return f'<a href="{escape(url, quote=True)}">{escape(f"@{acct}")}</a>'
    return hbold(f"@{acct}")


def _is_safe_http_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def account_event_title(
    event: str,
    account: dict[str, Any],
    auto_rejected: bool = False,
    auto_banned: bool = False,
) -> str:
    if event == "account.rejected":
        if auto_rejected:
            return f"{EMOJI_AUTO_BLOCKED} Auto-rejected account"
        return f"{EMOJI_BLOCKED} Rejected account"
    if event == "account.approved" or account.get("approved") is True:
        return f"{EMOJI_ACCEPTED} Approved account"
    if event == "account.created":
        if auto_banned:
            return f"{EMOJI_AUTO_BLOCKED} Auto-blocked registration"
        return f"{EMOJI_PENDING} New pending registration"
    return f"Mastodon {event}"


def render_account_event(
    event: str,
    account: dict[str, Any],
    auto_rejected: bool = False,
    auto_banned: bool = False,
    ip_geo: str = "",
) -> str:
    title = account_event_title(event, account, auto_rejected, auto_banned)
    invite_request = _bounded(account.get("invite_request") or "", _REASON_LIMIT)
    email = _bounded(account.get("email") or "unknown", _EMAIL_LIMIT)
    ip = _bounded(account.get("ip") or "unknown", _IP_LIMIT)
    locale = _bounded(account.get("locale") or "unknown", _LOCALE_LIMIT)
    lines = [
        hbold(title),
        f"Account: {admin_account_link(account)}",
        f"Email: {escape(str(email))}",
    ]
    if event != "account.created":
        lines.append(f"Approved: {_yes_no(account.get('approved'))}")
    ip_line = f"IP: {escape(str(ip))}"
    if ip_geo:
        ip_line += f" ({escape(_bounded(ip_geo, _IP_GEO_LIMIT))})"
    lines.append(ip_line)
    lines.append(f"Locale: {escape(str(locale))}")
    if invite_request:
        lines.append(f"Reason: {escape(str(invite_request))}")
    return "\n".join(lines)


def render_report_event(report: dict[str, Any]) -> str:
    report_id = _bounded(report.get("id", "unknown"), _REPORT_ID_LIMIT)
    reporter_account = report.get("account")
    target_account = report.get("target_account")
    reporter = admin_account_link(reporter_account if isinstance(reporter_account, dict) else None)
    target = admin_account_link(target_account if isinstance(target_account, dict) else None)
    category = _bounded(report.get("category") or "unknown", _CATEGORY_LIMIT)
    comment = _bounded(report.get("comment") or "", _COMMENT_LIMIT)
    raw_rules = report.get("rules")
    rules: list[Any] = raw_rules if isinstance(raw_rules, list) else []
    raw_statuses = report.get("statuses")
    statuses: list[Any] = raw_statuses if isinstance(raw_statuses, list) else []
    action_taken = report.get("action_taken")
    state = "resolved" if action_taken is True else "open"

    lines = [
        hbold(f"{EMOJI_REPORT} New Mastodon report"),
        f"Report: {hcode(report_id)}",
        f"Reporter: {reporter}",
        f"Target: {target}",
        f"Category: {escape(category)}",
        f"State: {escape(state)}",
    ]
    if _is_remote_account(target_account if isinstance(target_account, dict) else None):
        lines.append(f"Forwarded to remote: {_yes_no(report.get('forwarded'))}")
    if comment:
        lines.append(f"Comment: {escape(comment)}")
    if rules:
        rule_text = _bounded(
            ", ".join(
                str(rule.get("text") or rule.get("id"))
                for rule in rules
                if isinstance(rule, dict)
            ),
            _RULES_LIMIT,
        )
        if rule_text:
            lines.append(f"Rules: {escape(rule_text)}")
    for status in statuses[:3]:
        if isinstance(status, dict):
            lines.append("")
            lines.append(_render_status_summary(status))
    if len(statuses) > 3:
        lines.append(f"\n+{len(statuses) - 3} more attached statuses")
    return "\n".join(lines)


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _render_status_summary(status: dict[str, Any]) -> str:
    summary = escape(summarize_status(status, include_url=False))
    url = status.get("url") or status.get("uri")
    if isinstance(url, str) and len(url) <= _URL_LIMIT and _is_safe_http_url(url):
        return f'{summary}\n<a href="{escape(url, quote=True)}">Link↗</a>'
    return summary


def _is_remote_account(admin_account: dict[str, Any] | None) -> bool:
    if not admin_account:
        return False
    domain = admin_account.get("domain")
    if domain:
        return True
    account = admin_account.get("account")
    return isinstance(account, dict) and "@" in str(account.get("acct") or "")


def account_from_snapshot(snapshot: dict[str, str], *, approved: bool) -> dict[str, Any]:
    """Rebuild an admin account dict from a stored registration snapshot.

    The Mastodon approve/reject API responses do not reliably include the
    account's display fields, so updated messages are rendered from the
    snapshot instead.
    """
    account: dict[str, Any] = {}
    acct = snapshot.get("acct", "")
    if acct:
        account = {"acct": acct}
    return {
        "approved": approved,
        "email": snapshot.get("email", ""),
        "ip": snapshot.get("ip", ""),
        "locale": snapshot.get("locale", ""),
        "account": account,
        "invite_request": snapshot.get("reason", ""),
    }
