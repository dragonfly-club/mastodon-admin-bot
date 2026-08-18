from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING, Any

import regex
from aiogram.utils.markdown import hcode

if TYPE_CHECKING:
    from mastodon_admin_bot.storage.models import BlocklistRule
    from mastodon_admin_bot.storage.repository import Repository

logger = logging.getLogger(__name__)
REGEX_TIMEOUT_SECONDS = 0.1
MAX_PATTERN_LENGTH = 1024

RULE_TYPE_EMAIL = "email"
RULE_TYPE_EMAIL_DOMAIN = "email_domain"
RULE_TYPE_REASON = "reason"
RULE_TYPE_USED_REASON = "used_reason"

_RULE_TYPE_LABELS: dict[str, str] = {
    RULE_TYPE_EMAIL: "email",
    RULE_TYPE_EMAIL_DOMAIN: "email domain",
    RULE_TYPE_REASON: "reason",
    RULE_TYPE_USED_REASON: "used reason",
}

_MATCH_ORDER: tuple[str, ...] = (
    RULE_TYPE_EMAIL,
    RULE_TYPE_EMAIL_DOMAIN,
    RULE_TYPE_REASON,
    RULE_TYPE_USED_REASON,
)


@dataclass(frozen=True)
class AutobanInfo:
    matched_rule_type: str | None = None
    matched_pattern: str | None = None
    auto_reject_at: datetime | None = None
    # Whether the notification for this account should be delivered silently.
    silent: bool = True


def rule_type_label(rule_type: str) -> str:
    return _RULE_TYPE_LABELS.get(rule_type, rule_type)


def snapshot_account(account: dict[str, Any], ip_geo: str = "") -> dict[str, str]:
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
    snapshot = {
        "email": email,
        "email_domain": email_domain,
        "reason": reason,
        "acct": acct,
        "ip": ip,
        "locale": locale,
    }
    if ip_geo:
        snapshot["ip_geo"] = ip_geo
    return snapshot


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
    if rule_type == RULE_TYPE_USED_REASON:
        return snapshot.get("reason", "").strip()
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
                compiled = compile_rule_pattern(rule.pattern)
            except regex.error:
                logger.warning(
                    "Skipping invalid blocklist regex",
                    extra={"rule_type": rule.rule_type, "pattern": rule.pattern},
                )
                continue
            try:
                if compiled.search(value, timeout=REGEX_TIMEOUT_SECONDS):
                    return rule
            except TimeoutError:
                logger.warning(
                    "Blocklist regex timed out",
                    extra={"rule_id": rule.id, "rule_type": rule.rule_type},
                )
    return None


def compile_rule_pattern(pattern: str) -> regex.Pattern[str]:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise regex.error(f"pattern exceeds {MAX_PATTERN_LENGTH} characters")
    return regex.compile(pattern)


def used_reason_pattern(reason: str) -> str:
    """Anchored, case-insensitive exact-match pattern for a used invite reason."""
    return "(?i)^" + regex.escape(reason.strip()) + "$"


def used_reason_display(pattern: str) -> str:
    """Reverse the :func:`used_reason_pattern` encoding for display purposes."""
    prefix = "(?i)^"
    if pattern.startswith(prefix) and pattern.endswith("$") and len(pattern) > len(prefix) + 1:
        return regex.sub(r"\\(.)", r"\1", pattern[len(prefix) : -1])
    return pattern


def fit_reason_to_pattern_limit(reason: str) -> str:
    """Shorten ``reason`` (prefix-wise) until its pattern fits the length limit."""
    if len(used_reason_pattern(reason)) <= MAX_PATTERN_LENGTH:
        return reason
    lo, hi = 0, len(reason)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(used_reason_pattern(reason[:mid])) <= MAX_PATTERN_LENGTH:
            lo = mid
        else:
            hi = mid - 1
    return reason[:lo].rstrip()


async def record_used_reason(
    repository: Repository,
    reason: str,
    created_by: int | None = None,
) -> bool:
    """Record a rejected registration's invite reason for future auto-rejection.

    Reasons longer than the pattern limit are truncated to a prefix. Returns
    True when a new rule was created, False when the reason was empty or a
    rule for it already exists.
    """
    reason = fit_reason_to_pattern_limit(reason.strip())
    if not reason:
        return False
    _, created = await repository.add_blocklist_rule(
        rule_type=RULE_TYPE_USED_REASON,
        pattern=used_reason_pattern(reason),
        created_by=created_by,
    )
    return created


async def record_used_reason_for_account(
    repository: Repository,
    account_id: str,
    created_by: int | None = None,
) -> bool:
    """Record the stored invite reason of a rejected pending account, if any."""
    pending = await repository.get_pending_account(account_id)
    if pending is None:
        return False
    reason = snapshot_from_json(pending.account_snapshot).get("reason", "").strip()
    if not reason:
        return False
    return await record_used_reason(repository, reason, created_by)


def render_match_line(rule_type: str, pattern: str) -> str:
    label = escape(rule_type_label(rule_type))
    if rule_type == RULE_TYPE_USED_REASON:
        pattern = used_reason_display(pattern)
    rendered_pattern = hcode(pattern)
    return f"Autoban: {label} pattern {rendered_pattern} matched"


def render_auto_reject_at_line(auto_reject_at: datetime) -> str:
    local_time = auto_reject_at.astimezone()
    return f"Auto-reject at: {escape(local_time.strftime('%Y-%m-%d %H:%M:%S %:z'))}"
