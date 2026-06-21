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


def admin_account_link(admin_account: dict[str, Any] | None) -> str:
    acct = account_acct(admin_account)
    url = account_url(admin_account)
    if url and _is_safe_http_url(url):
        return f'<a href="{escape(url, quote=True)}">{escape(f"@{acct}")}</a>'
    return hbold(f"@{acct}")


def _is_safe_http_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def render_account_event(event: str, account: dict[str, Any]) -> str:
    title = "New pending registration" if event == "account.created" else f"Mastodon {event}"
    invite_request = account.get("invite_request") or ""
    email = account.get("email") or "unknown"
    ip = account.get("ip") or "unknown"
    locale = account.get("locale") or "unknown"
    lines = [
        hbold(title),
        f"Account: {admin_account_link(account)}",
        f"Email: {escape(str(email))}",
    ]
    if event != "account.created":
        lines.append(f"Approved: {_yes_no(account.get('approved'))}")
    lines.extend(
        [
            f"IP: {escape(str(ip))}",
            f"Locale: {escape(str(locale))}",
        ]
    )
    if invite_request:
        lines.append(f"Reason: {escape(str(invite_request))}")
    return "\n".join(lines)


def render_report_event(report: dict[str, Any]) -> str:
    report_id = str(report.get("id", "unknown"))
    reporter_account = report.get("account")
    target_account = report.get("target_account")
    reporter = admin_account_link(reporter_account if isinstance(reporter_account, dict) else None)
    target = admin_account_link(target_account if isinstance(target_account, dict) else None)
    category = str(report.get("category") or "unknown")
    comment = str(report.get("comment") or "")
    raw_rules = report.get("rules")
    rules: list[Any] = raw_rules if isinstance(raw_rules, list) else []
    raw_statuses = report.get("statuses")
    statuses: list[Any] = raw_statuses if isinstance(raw_statuses, list) else []
    action_taken = report.get("action_taken")
    state = "resolved" if action_taken is True else "open"

    lines = [
        hbold("New Mastodon report"),
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
        rule_text = ", ".join(
            str(rule.get("text") or rule.get("id")) for rule in rules if isinstance(rule, dict)
        )
        if rule_text:
            lines.append(f"Rules: {escape(rule_text)}")
    for status in statuses[:3]:
        if isinstance(status, dict):
            lines.append("")
            lines.append(escape(summarize_status(status)))
    if len(statuses) > 3:
        lines.append(f"\n+{len(statuses) - 3} more attached statuses")
    return "\n".join(lines)


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _is_remote_account(admin_account: dict[str, Any] | None) -> bool:
    if not admin_account:
        return False
    domain = admin_account.get("domain")
    if domain:
        return True
    account = admin_account.get("account")
    return isinstance(account, dict) and "@" in str(account.get("acct") or "")
