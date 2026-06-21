from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any
from urllib.parse import urlencode

import httpx
from aiogram import Bot, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message

from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.mastodon.client import MastodonApiError, MastodonClient
from mastodon_admin_bot.security import make_state
from mastodon_admin_bot.storage.repository import Repository

from .keyboards import Action, AdminCallback, open_keyboard
from .render import render_account_event, render_report_event

logger = logging.getLogger(__name__)
_ACTION_LOCKS = KeyedAsyncLocks()
_HANDLED_ACTION_KEYS: set[str] = set()


def build_router(
    settings: Settings,
    repository: Repository,
    action_locks: KeyedAsyncLocks = _ACTION_LOCKS,
    handled_action_keys: set[str] = _HANDLED_ACTION_KEYS,
) -> Router:
    router = Router(name=__name__)

    def is_trusted_user(user_id: int | None) -> bool:
        return user_id is not None and user_id in settings.trusted_telegram_user_ids

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if not is_trusted_user(message.from_user.id if message.from_user else None):
            await message.answer("This bot is restricted to trusted moderators.")
            return
        await message.answer("Use /link to connect your Mastodon moderator account.")

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_trusted_user(user_id) or user_id is None:
            await message.answer("This bot is restricted to trusted moderators.")
            return
        if not _is_private_chat(message.chat.type):
            await message.answer("Please DM this bot and run /whoami there.")
            return
        username = await repository.get_moderator_username(user_id)
        text = f"Linked Mastodon account: {username}" if username else "No Mastodon account linked."
        await message.answer(text)

    @router.message(Command("link"))
    async def link(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_trusted_user(user_id) or user_id is None:
            await message.answer("This bot is restricted to trusted moderators.")
            return
        if not _is_private_chat(message.chat.type):
            await message.answer(
                "Please DM this bot and run /link there to connect your Mastodon account."
            )
            return
        state = make_state()
        await repository.create_oauth_state(state, user_id)
        params = urlencode(
            {
                "response_type": "code",
                "client_id": settings.mastodon_client_id.get_secret_value(),
                "redirect_uri": str(settings.mastodon_redirect_uri),
                "scope": settings.mastodon_scopes,
                "state": state,
                "force_login": "true",
            }
        )
        await message.answer(
            f"Authorize Mastodon access:\n{settings.mastodon_origin}/oauth/authorize?{params}"
        )

    @router.callback_query(AdminCallback.filter())
    async def admin_callback(
        query: CallbackQuery,
        callback_data: AdminCallback,
        bot: Bot,
    ) -> None:
        user_id = query.from_user.id if query.from_user else None
        if not is_trusted_user(user_id) or user_id is None:
            await query.answer("Not authorized.", show_alert=True)
            return

        token_data = await repository.get_moderator_token(user_id)
        if token_data is None:
            await query.answer("Run /link first.", show_alert=True)
            return
        token, mastodon_username = token_data

        async def execute_action() -> dict[str, Any]:
            async with MastodonClient(settings.mastodon_origin, token=token) as client:
                return await _execute_action(client, callback_data)

        error_message, api_result = await _run_locked_action_result(
            action_locks,
            handled_action_keys,
            _action_lock_key(callback_data),
            execute_action,
        )
        if error_message is not None:
            await query.answer(error_message, show_alert=True)
            return

        await query.answer("Done.")
        if query.message and not isinstance(query.message, InaccessibleMessage):
            current_text = query.message.html_text or query.message.text or ""
            open_markup = _open_markup(settings.mastodon_origin, callback_data)
            await _mark_current_message_handled(
                bot=bot,
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=_action_result_text(
                    current_text=current_text,
                    callback_data=callback_data,
                    api_result=api_result,
                    mastodon_username=mastodon_username,
                ),
                reply_markup=open_markup,
            )
            for mapping in await repository.get_message_mappings(
                object_type=_callback_mapping_object_type(callback_data),
                object_id=callback_data.object_id,
            ):
                if (
                    mapping.chat_id == query.message.chat.id
                    and mapping.message_id == query.message.message_id
                ):
                    continue
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=mapping.chat_id,
                        message_id=mapping.message_id,
                        reply_markup=open_markup,
                    )
                except TelegramAPIError:
                    logger.warning(
                        "Failed to remove moderation keyboard",
                        extra={"chat_id": mapping.chat_id, "message_id": mapping.message_id},
                    )

    async def _execute_action(
        client: MastodonClient,
        callback_data: AdminCallback,
    ) -> dict[str, Any]:
        match callback_data.action:
            case Action.APPROVE_ACCOUNT:
                return await client.approve_account(callback_data.object_id)
            case Action.REJECT_ACCOUNT:
                return await client.reject_account(callback_data.object_id)
            case Action.RESOLVE_REPORT:
                return await client.resolve_report(callback_data.object_id)
            case Action.LIMIT_TARGET:
                if callback_data.target_id is None:
                    raise MastodonApiError(400, "missing target account id")
                return await client.account_action(
                    account_id=callback_data.target_id,
                    action_type="silence",
                    report_id=callback_data.object_id,
                    text="Action taken from Telegram moderation bot.",
                )
            case Action.SUSPEND_TARGET:
                if callback_data.target_id is None:
                    raise MastodonApiError(400, "missing target account id")
                return await client.account_action(
                    account_id=callback_data.target_id,
                    action_type="suspend",
                    report_id=callback_data.object_id,
                    text="Action taken from Telegram moderation bot.",
                )

    return router


def _is_private_chat(chat_type: str | ChatType) -> bool:
    return chat_type == ChatType.PRIVATE


async def _mark_current_message_handled(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramAPIError as exc:
        if _is_message_not_modified(exc):
            return
        logger.warning(
            "Failed to mark moderation message handled",
            extra={"chat_id": chat_id, "message_id": message_id},
        )


def _is_message_not_modified(exc: TelegramAPIError) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in exc.message.lower()


async def _run_action(
    execute: Callable[[], Awaitable[Any]],
) -> str | None:
    error_message, _result = await _run_action_result(execute)
    return error_message


async def _run_action_result(
    execute: Callable[[], Awaitable[Any]],
) -> tuple[str | None, Any | None]:
    try:
        result = await execute()
    except MastodonApiError as exc:
        return f"Mastodon rejected action: {exc.message}", None
    except (httpx.HTTPError, ValueError, KeyError):
        return "Mastodon action failed. Please retry.", None
    except Exception:
        logger.exception("Unexpected moderation action failure")
        return "Unexpected moderation action failure. Please retry.", None
    return None, result


async def _run_locked_action(
    locks: KeyedAsyncLocks,
    handled_keys: set[str],
    lock_key: str,
    execute: Callable[[], Awaitable[Any]],
) -> str | None:
    error_message, _result = await _run_locked_action_result(locks, handled_keys, lock_key, execute)
    return error_message


async def _run_locked_action_result(
    locks: KeyedAsyncLocks,
    handled_keys: set[str],
    lock_key: str,
    execute: Callable[[], Awaitable[Any]],
) -> tuple[str | None, Any | None]:
    lock = await locks.try_acquire(lock_key)
    if lock is None:
        return "That moderation decision is already being handled.", None
    async with lock:
        if lock_key in handled_keys:
            return "That moderation decision was already handled.", None
        error_message, result = await _run_action_result(execute)
        if error_message is None:
            handled_keys.add(lock_key)
        return error_message, result


def _action_result_text(
    *,
    current_text: str,
    callback_data: AdminCallback,
    api_result: Any,
    mastodon_username: str,
) -> str:
    if isinstance(api_result, dict) and api_result:
        match _callback_mapping_object_type(callback_data):
            case "account":
                text = render_account_event(_account_result_event(callback_data.action), api_result)
            case "report":
                text = render_report_event(api_result)
    else:
        text = current_text
    return f"{text}{_handled_suffix(mastodon_username, callback_data.action)}"


def _account_result_event(action: Action) -> str:
    match action:
        case Action.APPROVE_ACCOUNT:
            return "account.approved"
        case Action.REJECT_ACCOUNT:
            return "account.rejected"
        case _:
            raise ValueError(f"unsupported account action: {action}")


def _open_markup(
    mastodon_origin: str,
    callback_data: AdminCallback,
) -> InlineKeyboardMarkup | None:
    if callback_data.action == Action.REJECT_ACCOUNT:
        return None
    return open_keyboard(_open_url(mastodon_origin, callback_data))


def _open_url(mastodon_origin: str, callback_data: AdminCallback) -> str:
    origin = mastodon_origin.rstrip("/")
    match _callback_mapping_object_type(callback_data):
        case "account":
            return f"{origin}/admin/accounts/{callback_data.object_id}"
        case "report":
            return f"{origin}/admin/reports/{callback_data.object_id}"
    raise ValueError(f"unsupported callback action: {callback_data.action}")


def _handled_suffix(mastodon_username: str, action: Action) -> str:
    return f"\n\nHandled by {escape(mastodon_username)}: {_action_label(action)}"


def _action_label(action: Action) -> str:
    match action:
        case Action.APPROVE_ACCOUNT:
            return "Approved account"
        case Action.REJECT_ACCOUNT:
            return "Rejected account"
        case Action.RESOLVE_REPORT:
            return "Resolved report"
        case Action.LIMIT_TARGET:
            return "Limited target account"
        case Action.SUSPEND_TARGET:
            return "Suspended target account"


def _callback_mapping_object_type(callback_data: AdminCallback) -> str:
    match callback_data.action:
        case Action.APPROVE_ACCOUNT | Action.REJECT_ACCOUNT:
            return "account"
        case Action.RESOLVE_REPORT | Action.LIMIT_TARGET | Action.SUSPEND_TARGET:
            return "report"


def _action_lock_key(callback_data: AdminCallback) -> str:
    match callback_data.action:
        case Action.APPROVE_ACCOUNT | Action.REJECT_ACCOUNT:
            return f"account_decision:{callback_data.object_id}"
        case Action.RESOLVE_REPORT:
            return f"report_state:{callback_data.object_id}"
        case Action.LIMIT_TARGET | Action.SUSPEND_TARGET:
            target_id = callback_data.target_id or "unknown"
            return f"report_target_action:{callback_data.object_id}:{target_id}"
