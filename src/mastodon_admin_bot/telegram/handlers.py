from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from urllib.parse import urlencode

import httpx
from aiogram import Bot, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.mastodon.client import MastodonApiError, MastodonClient
from mastodon_admin_bot.security import make_state
from mastodon_admin_bot.storage.repository import Repository

from .keyboards import Action, AdminCallback

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

        async def execute_action() -> None:
            async with MastodonClient(settings.mastodon_origin, token=token) as client:
                await _execute_action(client, callback_data)

        error_message = await _run_locked_action(
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
            suffix = _handled_suffix(mastodon_username, callback_data.action)
            current_text = query.message.html_text or query.message.text or ""
            await _mark_current_message_handled(
                bot=bot,
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=f"{current_text}{suffix}",
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
                        reply_markup=None,
                    )
                except TelegramAPIError:
                    logger.warning(
                        "Failed to remove moderation keyboard",
                        extra={"chat_id": mapping.chat_id, "message_id": mapping.message_id},
                    )

    async def _execute_action(client: MastodonClient, callback_data: AdminCallback) -> None:
        match callback_data.action:
            case Action.APPROVE_ACCOUNT:
                await client.approve_account(callback_data.object_id)
            case Action.REJECT_ACCOUNT:
                await client.reject_account(callback_data.object_id)
            case Action.RESOLVE_REPORT:
                await client.resolve_report(callback_data.object_id)
            case Action.LIMIT_TARGET:
                if callback_data.target_id is None:
                    raise MastodonApiError(400, "missing target account id")
                await client.account_action(
                    account_id=callback_data.target_id,
                    action_type="silence",
                    report_id=callback_data.object_id,
                    text="Action taken from Telegram moderation bot.",
                )
            case Action.SUSPEND_TARGET:
                if callback_data.target_id is None:
                    raise MastodonApiError(400, "missing target account id")
                await client.account_action(
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
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
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
    execute: Callable[[], Awaitable[None]],
) -> str | None:
    try:
        await execute()
    except MastodonApiError as exc:
        return f"Mastodon rejected action: {exc.message}"
    except (httpx.HTTPError, ValueError, KeyError):
        return "Mastodon action failed. Please retry."
    except Exception:
        logger.exception("Unexpected moderation action failure")
        return "Unexpected moderation action failure. Please retry."
    return None


async def _run_locked_action(
    locks: KeyedAsyncLocks,
    handled_keys: set[str],
    lock_key: str,
    execute: Callable[[], Awaitable[None]],
) -> str | None:
    lock = await locks.try_acquire(lock_key)
    if lock is None:
        return "That moderation decision is already being handled."
    async with lock:
        if lock_key in handled_keys:
            return "That moderation decision was already handled."
        error_message = await _run_action(execute)
        if error_message is None:
            handled_keys.add(lock_key)
        return error_message


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
