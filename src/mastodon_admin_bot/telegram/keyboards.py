from collections.abc import Iterable
from enum import StrEnum

import regex
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from mastodon_admin_bot.autoban import (
    RULE_TYPE_EMAIL,
    RULE_TYPE_EMAIL_DOMAIN,
    RULE_TYPE_REASON,
    RULE_TYPE_USED_REASON,
    used_reason_pattern,
)
from mastodon_admin_bot.storage.repository import Repository


class Action(StrEnum):
    APPROVE_ACCOUNT = "ao"
    REJECT_ACCOUNT = "an"
    RESOLVE_REPORT = "rr"
    LIMIT_TARGET = "al"
    SUSPEND_TARGET = "au"
    FORCE_APPROVE_ACCOUNT = "af"
    REJECT_NOW_ACCOUNT = "rn"
    BLOCK_EMAIL = "be"
    BLOCK_EMAIL_DOMAIN = "bd"
    BLOCK_REASON = "br"


BLOCK_ACTIONS: frozenset[Action] = frozenset(
    {Action.BLOCK_EMAIL, Action.BLOCK_EMAIL_DOMAIN, Action.BLOCK_REASON}
)
BLOCK_ACTION_TO_RULE_TYPE: dict[Action, str] = {
    Action.BLOCK_EMAIL: RULE_TYPE_EMAIL,
    Action.BLOCK_EMAIL_DOMAIN: RULE_TYPE_EMAIL_DOMAIN,
    Action.BLOCK_REASON: RULE_TYPE_REASON,
}
BLOCK_ACTION_TO_SNAPSHOT_FIELD: dict[Action, str] = {
    Action.BLOCK_EMAIL: "email",
    Action.BLOCK_EMAIL_DOMAIN: "email_domain",
    Action.BLOCK_REASON: "reason",
}


def block_rule_pattern(value: str) -> str:
    """Build the anchored, exact-match regex pattern used for a block rule."""
    return "^" + regex.escape(value) + "$"


async def applied_block_actions(
    repository: Repository,
    snapshot: dict[str, str],
) -> set[Action]:
    """Return block actions whose rule already exists for this account's values.

    Each block button maps to a rule pattern derived from the account's
    snapshot. A button stays hidden as long as its rule exists, so re-rendering
    the keyboard after any block action removes every button that has already
    been used, not just the one just clicked. The reason button also counts as
    applied when the exact invite reason was already recorded as a used reason.
    """
    rules = await repository.list_blocklist_rules()
    existing = {(rule.rule_type, rule.pattern) for rule in rules}
    applied: set[Action] = set()
    for action in BLOCK_ACTIONS:
        value = snapshot.get(BLOCK_ACTION_TO_SNAPSHOT_FIELD[action], "")
        if not value:
            continue
        if (BLOCK_ACTION_TO_RULE_TYPE[action], block_rule_pattern(value)) in existing:
            applied.add(action)
    reason = snapshot.get("reason", "").strip()
    if reason and (RULE_TYPE_USED_REASON, used_reason_pattern(reason)) in existing:
        applied.add(Action.BLOCK_REASON)
    return applied


class AdminCallback(CallbackData, prefix="a"):
    action: Action
    object_id: str
    target_id: str | None = None


class BlocklistPageCallback(CallbackData, prefix="bl"):
    page: int


def blocklist_page_keyboard(page: int, page_count: int) -> InlineKeyboardMarkup | None:
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="Prev", callback_data=BlocklistPageCallback(page=page - 1).pack()
            )
        )
    if page + 1 < page_count:
        buttons.append(
            InlineKeyboardButton(
                text="Next", callback_data=BlocklistPageCallback(page=page + 1).pack()
            )
        )
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def account_keyboard(
    account_id: str,
    url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Approve",
        callback_data=AdminCallback(
            action=Action.APPROVE_ACCOUNT,
            object_id=account_id,
        ),
    )
    builder.button(
        text="Reject",
        callback_data=AdminCallback(
            action=Action.REJECT_ACCOUNT,
            object_id=account_id,
        ),
    )
    if url:
        builder.button(text="Open", url=url)
    builder.adjust(2, 1)
    return builder.as_markup()


def autoban_keyboard(account_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Force Approve",
        callback_data=AdminCallback(
            action=Action.FORCE_APPROVE_ACCOUNT,
            object_id=account_id,
        ),
    )
    builder.button(
        text="Reject Now",
        callback_data=AdminCallback(
            action=Action.REJECT_NOW_ACCOUNT,
            object_id=account_id,
        ),
    )
    builder.adjust(2)
    return builder.as_markup()


def _excluded_actions(exclude: Action | Iterable[Action] | None) -> frozenset[Action]:
    if exclude is None:
        return frozenset()
    if isinstance(exclude, Action):
        return frozenset((exclude,))
    return frozenset(exclude)


def post_rejection_keyboard(
    account_id: str,
    *,
    include_reason: bool = False,
    exclude: Action | Iterable[Action] | None = None,
) -> InlineKeyboardMarkup:
    excluded = _excluded_actions(exclude)
    builder = InlineKeyboardBuilder()
    if Action.BLOCK_EMAIL not in excluded:
        builder.button(
            text="Block Email",
            callback_data=AdminCallback(
                action=Action.BLOCK_EMAIL,
                object_id=account_id,
            ),
        )
    if Action.BLOCK_EMAIL_DOMAIN not in excluded:
        builder.button(
            text="Block Domain",
            callback_data=AdminCallback(
                action=Action.BLOCK_EMAIL_DOMAIN,
                object_id=account_id,
            ),
        )
    if include_reason and Action.BLOCK_REASON not in excluded:
        builder.button(
            text="Block Reason",
            callback_data=AdminCallback(
                action=Action.BLOCK_REASON,
                object_id=account_id,
            ),
        )
    builder.adjust(2, 1)
    return builder.as_markup()


def open_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Open", url=url)
    return builder.as_markup()


def report_keyboard(
    report_id: str,
    target_account_id: str | None,
    url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Resolve",
        callback_data=AdminCallback(
            action=Action.RESOLVE_REPORT,
            object_id=report_id,
        ),
    )
    if target_account_id:
        builder.button(
            text="Limit target",
            callback_data=AdminCallback(
                action=Action.LIMIT_TARGET,
                object_id=report_id,
                target_id=target_account_id,
            ),
        )
        builder.button(
            text="Suspend target",
            callback_data=AdminCallback(
                action=Action.SUSPEND_TARGET,
                object_id=report_id,
                target_id=target_account_id,
            ),
        )
    if url:
        builder.button(text="Open", url=url)
    builder.adjust(1, 2, 1)
    return builder.as_markup()
