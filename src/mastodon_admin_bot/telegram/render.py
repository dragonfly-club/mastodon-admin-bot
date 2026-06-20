from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlsplit

from aiogram.utils.markdown import hbold, hcode

from mastodon_admin_bot.mastodon.webhooks import (
    account_acct,
    account_url,
    html_to_text,
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
    locale = account.get("locale") or "unknown"
    lines = [
        hbold(title),
        f"Account: {admin_account_link(account)}",
        f"ID: {hcode(str(account.get('id', 'unknown')))}",
        f"Email: {escape(str(email))}",
        f"Locale: {escape(str(locale))}",
    ]
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

    lines = [
        hbold("New Mastodon report"),
        f"Report: {hcode(report_id)}",
        f"Reporter: {reporter}",
        f"Target: {target}",
        f"Category: {escape(category)}",
    ]
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


def render_status_event(status: dict[str, Any], event: str) -> str:
    content = html_to_text(str(status.get("content") or ""))
    url = status.get("url") or status.get("uri")
    lines = [hbold(f"Mastodon {event}"), escape(content or "<empty status>")]
    if url:
        lines.append(escape(str(url)))
    return "\n".join(lines)
