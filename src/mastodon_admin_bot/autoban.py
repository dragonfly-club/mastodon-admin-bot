from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING, Any

from aiogram.utils.markdown import hcode

if TYPE_CHECKING:
    from mastodon_admin_bot.storage.models import BlocklistRule
    from mastodon_admin_bot.storage.repository import Repository

logger = logging.getLogger(__name__)

RULE_TYPE_EMAIL = "email"
RULE_TYPE_EMAIL_DOMAIN = "email_domain"
RULE_TYPE_REASON = "reason"

_RULE_TYPE_LABELS: dict[str, str] = {
    RULE_TYPE_EMAIL: "email",
    RULE_TYPE_EMAIL_DOMAIN: "email domain",
    RULE_TYPE_REASON: "reason",
}

_MATCH_ORDER: tuple[str, ...] = (
    RULE_TYPE_EMAIL,
    RULE_TYPE_EMAIL_DOMAIN,
    RULE_TYPE_REASON,
)


@dataclass(frozen=True)
class AutobanInfo:
    matched_rule_type: str | None = None
    matched_pattern: str | None = None
    auto_reject_at: datetime | None = None


def rule_type_label(rule_type: str) -> str:
    return _RULE_TYPE_LABELS.get(rule_type, rule_type)


def snapshot_account(account: dict[str, Any]) -> dict[str, str]:
    email = str(account.get("email") or "")
    email_domain = ""
    if "@" in email:
        email_domain = email.rsplit("@", 1)[1]
    reason = str(account.get("invite_request") or "")
    acct_value = account.get("account")
    if isinstance(acct_value, dict) and acct_value.get("acct"):
        acct = str(acct_value["acct"])
    else:
        acct = str(account.get("username") or "")
    ip = str(account.get("ip") or "")
    locale = str(account.get("locale") or "")
    return {
        "email": email,
        "email_domain": email_domain,
        "reason": reason,
        "acct": acct,
        "ip": ip,
        "locale": locale,
    }


def snapshot_to_json(snapshot: dict[str, str]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)


def snapshot_from_json(raw: str) -> dict[str, str]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _account_field_for_rule(rule_type: str, snapshot: dict[str, str]) -> str:
    if rule_type == RULE_TYPE_EMAIL:
        return snapshot.get("email", "")
    if rule_type == RULE_TYPE_EMAIL_DOMAIN:
        return snapshot.get("email_domain", "")
    if rule_type == RULE_TYPE_REASON:
        return snapshot.get("reason", "")
    return ""


async def find_match(
    repository: Repository,
    account: dict[str, Any],
) -> BlocklistRule | None:
    snapshot = snapshot_account(account)
    rules = await repository.list_blocklist_rules()
    by_type: dict[str, list[BlocklistRule]] = {rt: [] for rt in _MATCH_ORDER}
    for rule in rules:
        if rule.rule_type in by_type:
            by_type[rule.rule_type].append(rule)
    for rule_type in _MATCH_ORDER:
        value = _account_field_for_rule(rule_type, snapshot)
        if not value:
            continue
        for rule in by_type[rule_type]:
            try:
                compiled = re.compile(rule.pattern)
            except re.error:
                logger.warning(
                    "Skipping invalid blocklist regex",
                    extra={"rule_type": rule.rule_type, "pattern": rule.pattern},
                )
                continue
            if compiled.search(value):
                return rule
    return None


def render_match_line(rule_type: str, pattern: str) -> str:
    label = escape(rule_type_label(rule_type))
    rendered_pattern = hcode(pattern)
    return f"Autoban: {label} pattern {rendered_pattern} matched"


def render_auto_reject_at_line(auto_reject_at: datetime) -> str:
    return f"Auto-reject at: {escape(auto_reject_at.strftime('%Y-%m-%d %H:%M:%S UTC'))}"
