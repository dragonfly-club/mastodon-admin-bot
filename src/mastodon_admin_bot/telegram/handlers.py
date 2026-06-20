from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any
from urllib.parse import urlencode

import httpx
from aiogram import Bot, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.mastodon.client import MastodonApiError, MastodonClient
from mastodon_admin_bot.security import make_state
from mastodon_admin_bot.storage.repository import Repository

from .keyboards import Action, AdminCallback

logger = logging.getLogger(__name__)


def build_router(settings: Settings, repository: Repository) -> Router:
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
        webhook_event = await repository.get_webhook_event(callback_data.event_id)
        if webhook_event is None:
            await query.answer("This moderation event no longer exists.", show_alert=True)
            return
        payload = json.loads(webhook_event.raw_payload)
        if not isinstance(payload, dict) or not _callback_matches_event(callback_data, payload):
            await query.answer("This action does not match the original event.", show_alert=True)
            return

        lock_key = _action_lock_key(callback_data)
        object_type, action_object_id = _action_object(callback_data)
        action = await repository.try_create_action(
            event_id=callback_data.event_id,
            lock_key=lock_key,
            action_type=callback_data.action.value,
            object_type=object_type,
            object_id=action_object_id,
            telegram_user_id=user_id,
            mastodon_username=mastodon_username,
        )
        if action is None:
            await query.answer("That moderation decision was already handled.", show_alert=True)
            return

        async def execute_action() -> None:
            async with MastodonClient(settings.mastodon_origin, token=token) as client:
                await _execute_action(client, callback_data)

        error_message = await _run_action_with_lock_cleanup(repository, action.id, execute_action)
        if error_message is not None:
            await query.answer(error_message, show_alert=True)
            return

        await repository.mark_action_success(action.id)
        await query.answer("Done.")
        if query.message and not isinstance(query.message, InaccessibleMessage):
            suffix = _handled_suffix(mastodon_username, callback_data.action)
            current_text = query.message.html_text or query.message.text or ""
            await bot.edit_message_text(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=f"{current_text}{suffix}",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
                disable_web_page_preview=True,
            )

    async def _execute_action(client: MastodonClient, callback_data: AdminCallback) -> None:
        match callback_data.action:
            case Action.APPROVE_ACCOUNT:
                await client.approve_account(callback_data.object_id)
            case Action.REJECT_ACCOUNT:
                await client.reject_account(callback_data.object_id)
            case Action.ASSIGN_REPORT:
                await client.assign_report_to_self(callback_data.object_id)
            case Action.RESOLVE_REPORT:
                await client.resolve_report(callback_data.object_id)
            case Action.REOPEN_REPORT:
                await client.reopen_report(callback_data.object_id)
            case Action.SILENCE_TARGET:
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


async def _run_action_with_lock_cleanup(
    repository: Repository,
    action_id: int,
    execute: Callable[[], Awaitable[None]],
) -> str | None:
    try:
        await execute()
    except MastodonApiError as exc:
        await repository.mark_action_failed(action_id, exc.message)
        return f"Mastodon rejected action: {exc.message}"
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        await repository.mark_action_failed(action_id, str(exc))
        return "Mastodon action failed. Please retry."
    except Exception as exc:
        logger.exception("Unexpected moderation action failure")
        await repository.mark_action_failed(action_id, str(exc))
        return "Unexpected moderation action failure. Please retry."
    return None


def _handled_suffix(mastodon_username: str, action: Action) -> str:
    return f"\n\nHandled by {escape(mastodon_username)}: {action.value}"


def _callback_matches_event(callback_data: AdminCallback, payload: dict[str, Any]) -> bool:
    event = payload.get("event")
    obj = payload.get("object")
    if not isinstance(event, str) or not isinstance(obj, dict):
        return False
    match callback_data.action:
        case Action.APPROVE_ACCOUNT | Action.REJECT_ACCOUNT:
            return event.startswith("account.") and str(obj.get("id")) == callback_data.object_id
        case Action.ASSIGN_REPORT | Action.RESOLVE_REPORT | Action.REOPEN_REPORT:
            return event.startswith("report.") and str(obj.get("id")) == callback_data.object_id
        case Action.SILENCE_TARGET | Action.SUSPEND_TARGET:
            target = obj.get("target_account")
            target_id = (
                str(target.get("id")) if isinstance(target, dict) and target.get("id") else None
            )
            return (
                event.startswith("report.")
                and str(obj.get("id")) == callback_data.object_id
                and target_id == callback_data.target_id
            )


def _action_lock_key(callback_data: AdminCallback) -> str:
    match callback_data.action:
        case Action.APPROVE_ACCOUNT | Action.REJECT_ACCOUNT:
            return f"account_decision:{callback_data.object_id}"
        case Action.ASSIGN_REPORT:
            return f"report_assignment:{callback_data.object_id}"
        case Action.RESOLVE_REPORT | Action.REOPEN_REPORT:
            return f"report_state:{callback_data.object_id}:{callback_data.event_id}"
        case Action.SILENCE_TARGET | Action.SUSPEND_TARGET:
            return f"report_target_action:{callback_data.object_id}:{callback_data.target_id}"


def _action_object(callback_data: AdminCallback) -> tuple[str, str]:
    match callback_data.action:
        case Action.APPROVE_ACCOUNT | Action.REJECT_ACCOUNT:
            return "account", callback_data.object_id
        case Action.ASSIGN_REPORT | Action.RESOLVE_REPORT | Action.REOPEN_REPORT:
            return "report", callback_data.object_id
        case Action.SILENCE_TARGET | Action.SUSPEND_TARGET:
            if callback_data.target_id is None:
                return "account", callback_data.object_id
            return "account", callback_data.target_id
