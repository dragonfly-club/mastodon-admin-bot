from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


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


class AdminCallback(CallbackData, prefix="a"):
    action: Action
    object_id: str
    target_id: str | None = None


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


def post_rejection_keyboard(
    account_id: str,
    *,
    include_reason: bool = False,
    exclude: Action | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if exclude != Action.BLOCK_EMAIL:
        builder.button(
            text="Block Email",
            callback_data=AdminCallback(
                action=Action.BLOCK_EMAIL,
                object_id=account_id,
            ),
        )
    if exclude != Action.BLOCK_EMAIL_DOMAIN:
        builder.button(
            text="Block Domain",
            callback_data=AdminCallback(
                action=Action.BLOCK_EMAIL_DOMAIN,
                object_id=account_id,
            ),
        )
    if include_reason and exclude != Action.BLOCK_REASON:
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
