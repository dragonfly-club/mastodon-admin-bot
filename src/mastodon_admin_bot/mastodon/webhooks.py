from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class MastodonWebhook:
    event: str
    created_at: str | None
    object: dict[str, Any]
    payload: dict[str, Any]

    @property
    def object_id(self) -> str | None:
        value = self.object.get("id")
        return str(value) if value is not None else None


def parse_webhook_payload(payload: dict[str, Any]) -> MastodonWebhook:
    event = payload.get("event")
    if not isinstance(event, str) or not event:
        raise ValueError("webhook payload missing event")
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise ValueError("webhook payload missing object")
    created_at = payload.get("created_at")
    return MastodonWebhook(
        event=event,
        created_at=created_at if isinstance(created_at, str) else None,
        object=obj,
        payload=payload,
    )


def account_acct(admin_account: dict[str, Any] | None) -> str:
    if not admin_account:
        return "unknown"
    account = admin_account.get("account")
    if isinstance(account, dict) and account.get("acct"):
        return str(account["acct"])
    username = admin_account.get("username")
    domain = admin_account.get("domain")
    if username and domain:
        return f"{username}@{domain}"
    return str(username or "unknown")


def account_url(admin_account: dict[str, Any] | None) -> str | None:
    if not admin_account:
        return None
    account = admin_account.get("account")
    if isinstance(account, dict) and account.get("url"):
        return str(account["url"])
    return None


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    text = soup.get_text(" ", strip=True)
    return " ".join(text.split())


def summarize_status(status: dict[str, Any], limit: int = 280, include_url: bool = True) -> str:
    status_account = status.get("account")
    author = account_acct(status_account if isinstance(status_account, dict) else None)
    content = html_to_text(str(status.get("content") or ""))
    url = status.get("url") or status.get("uri")
    summary = f"@{author}: {content}" if content else f"@{author}: <empty status>"
    if len(summary) > limit:
        summary = f"{summary[: limit - 1]}…"
    if include_url and url:
        summary = f"{summary}\n{url}"
    return summary


def is_pending_local_account(obj: dict[str, Any]) -> bool:
    return obj.get("domain") is None and obj.get("approved") is False
