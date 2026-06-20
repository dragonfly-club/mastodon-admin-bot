from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class Action(StrEnum):
    APPROVE_ACCOUNT = "ao"
    REJECT_ACCOUNT = "an"
    ASSIGN_REPORT = "ra"
    RESOLVE_REPORT = "rr"
    REOPEN_REPORT = "ro"
    SILENCE_TARGET = "as"
    SUSPEND_TARGET = "au"


class AdminCallback(CallbackData, prefix="a"):
    action: Action
    object_id: str
    event_id: int
    target_id: str | None = None


def account_keyboard(
    account_id: str,
    event_id: int,
    url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Approve",
        callback_data=AdminCallback(
            action=Action.APPROVE_ACCOUNT,
            object_id=account_id,
            event_id=event_id,
        ),
    )
    builder.button(
        text="Reject",
        callback_data=AdminCallback(
            action=Action.REJECT_ACCOUNT,
            object_id=account_id,
            event_id=event_id,
        ),
    )
    if url:
        builder.button(text="Open", url=url)
    builder.adjust(2, 1)
    return builder.as_markup()


def report_keyboard(
    report_id: str,
    target_account_id: str | None,
    event_id: int,
    url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Assign to me",
        callback_data=AdminCallback(
            action=Action.ASSIGN_REPORT,
            object_id=report_id,
            event_id=event_id,
        ),
    )
    builder.button(
        text="Resolve",
        callback_data=AdminCallback(
            action=Action.RESOLVE_REPORT,
            object_id=report_id,
            event_id=event_id,
        ),
    )
    builder.button(
        text="Reopen",
        callback_data=AdminCallback(
            action=Action.REOPEN_REPORT,
            object_id=report_id,
            event_id=event_id,
        ),
    )
    if target_account_id:
        builder.button(
            text="Silence target",
            callback_data=AdminCallback(
                action=Action.SILENCE_TARGET,
                object_id=report_id,
                target_id=target_account_id,
                event_id=event_id,
            ),
        )
        builder.button(
            text="Suspend target",
            callback_data=AdminCallback(
                action=Action.SUSPEND_TARGET,
                object_id=report_id,
                target_id=target_account_id,
                event_id=event_id,
            ),
        )
    if url:
        builder.button(text="Open", url=url)
    builder.adjust(2, 1, 2, 1)
    return builder.as_markup()
