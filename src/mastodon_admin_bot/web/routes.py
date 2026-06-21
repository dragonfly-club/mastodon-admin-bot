from __future__ import annotations

import json
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from aiohttp import web

from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.mastodon.client import MastodonClient
from mastodon_admin_bot.mastodon.webhooks import is_pending_local_account, parse_webhook_payload
from mastodon_admin_bot.security import verify_mastodon_signature
from mastodon_admin_bot.storage.repository import Repository
from mastodon_admin_bot.telegram.keyboards import account_keyboard, report_keyboard
from mastodon_admin_bot.telegram.render import (
    render_account_event,
    render_report_event,
)

_WEBHOOK_LOCKS = KeyedAsyncLocks()


def build_routes(
    settings: Settings,
    repository: Repository,
    bot: Bot,
    webhook_locks: KeyedAsyncLocks = _WEBHOOK_LOCKS,
) -> web.RouteTableDef:
    routes = web.RouteTableDef()

    @routes.get("/healthz")
    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    @routes.post("/mastodon/webhook")
    async def mastodon_webhook(request: web.Request) -> web.Response:
        raw_body = await request.read()
        if not verify_mastodon_signature(
            raw_body,
            request.headers.get("X-Hub-Signature"),
            settings.mastodon_webhook_secret.get_secret_value(),
        ):
            return web.json_response({"error": "invalid signature"}, status=401)
        try:
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            event = parse_webhook_payload(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

        object_type = _object_type_for_event(event.event)
        if object_type is None or event.object_id is None:
            return web.json_response({"ok": True, "ignored": True})

        failed_chat_ids: list[int] = []
        if settings.telegram_home_chat_ids:
            for chat_id in settings.telegram_home_chat_ids:
                failed = await _deliver_event_to_chat(
                    repository=repository,
                    bot=bot,
                    webhook_locks=webhook_locks,
                    chat_id=chat_id,
                    object_type=object_type,
                    object_id=event.object_id,
                    event_name=event.event,
                    obj=event.object,
                    mastodon_origin=settings.mastodon_origin,
                )
                if failed:
                    failed_chat_ids.append(chat_id)
        status = 502 if failed_chat_ids else 200
        return web.json_response(
            {
                "ok": not failed_chat_ids,
                "failed_chat_ids": failed_chat_ids,
            },
            status=status,
        )

    @routes.get("/oauth/callback")
    async def oauth_callback(request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return web.Response(text="Missing code or state", status=400)

        telegram_user_id = await repository.consume_oauth_state(state)
        if telegram_user_id is None:
            return web.Response(text="Invalid or expired OAuth state", status=400)

        async with MastodonClient(settings.mastodon_origin) as client:
            token_response = await client.exchange_oauth_code(
                code=code,
                client_id=settings.mastodon_client_id.get_secret_value(),
                client_secret=settings.mastodon_client_secret.get_secret_value(),
                redirect_uri=str(settings.mastodon_redirect_uri),
            )
            access_token = str(token_response["access_token"])
            scopes = str(token_response.get("scope") or settings.mastodon_scopes)

        async with MastodonClient(settings.mastodon_origin, token=access_token) as user_client:
            credentials = await user_client.verify_credentials()

        mastodon_account_id = str(credentials.get("id"))
        mastodon_username = str(
            credentials.get("acct") or credentials.get("username") or mastodon_account_id
        )
        await repository.upsert_moderator_link(
            telegram_user_id=telegram_user_id,
            mastodon_account_id=mastodon_account_id,
            mastodon_username=mastodon_username,
            access_token=access_token,
            scopes=scopes,
        )
        return web.Response(
            text=f"Linked Mastodon account {mastodon_username}. You can close this page."
        )

    return routes


async def _send_event_message(
    *,
    bot: Bot,
    chat_id: int,
    event_name: str,
    obj: dict[str, Any],
    mastodon_origin: str,
) -> Any:
    text, keyboard = _render_event_message(event_name, obj, mastodon_origin)
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _deliver_event_to_chat(
    *,
    repository: Repository,
    bot: Bot,
    webhook_locks: KeyedAsyncLocks,
    chat_id: int,
    object_type: str,
    object_id: str,
    event_name: str,
    obj: dict[str, Any],
    mastodon_origin: str,
) -> bool:
    lock_key = _webhook_lock_key(object_type, object_id, chat_id)
    try:
        async with await webhook_locks.acquire(lock_key):
            mapping = await repository.get_message_mapping(
                object_type=object_type,
                object_id=object_id,
                chat_id=chat_id,
            )
            if mapping is None:
                if not _should_send_new_message(event_name, obj):
                    return False
                message = await _send_event_message(
                    bot=bot,
                    chat_id=chat_id,
                    event_name=event_name,
                    obj=obj,
                    mastodon_origin=mastodon_origin,
                )
                await repository.upsert_message_mapping(
                    object_type=object_type,
                    object_id=object_id,
                    chat_id=chat_id,
                    message_id=message.message_id,
                )
            else:
                await _edit_event_message(
                    bot=bot,
                    chat_id=chat_id,
                    message_id=mapping.message_id,
                    event_name=event_name,
                    obj=obj,
                    mastodon_origin=mastodon_origin,
                )
    except TelegramAPIError as exc:
        if _is_message_not_modified(exc):
            return False
        # Keep the existing mapping; a later webhook can retry the edit.
        return True
    return False


async def _edit_event_message(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    event_name: str,
    obj: dict[str, Any],
    mastodon_origin: str,
) -> Any:
    text, keyboard = _render_event_message(event_name, obj, mastodon_origin)
    return await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def _render_event_message(
    event_name: str,
    obj: dict[str, Any],
    mastodon_origin: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    if event_name.startswith("account."):
        text = render_account_event(event_name, obj)
        account_id = str(obj.get("id"))
        url = f"{mastodon_origin}/admin/accounts/{account_id}" if account_id else None
        keyboard = account_keyboard(account_id, url) if is_pending_local_account(obj) else None
    elif event_name.startswith("report."):
        text = render_report_event(obj)
        report_id = str(obj.get("id"))
        target = obj.get("target_account")
        target_id = str(target.get("id")) if isinstance(target, dict) and target.get("id") else None
        url = f"{mastodon_origin}/admin/reports/{report_id}"
        keyboard = (
            None
            if obj.get("action_taken") is True
            else report_keyboard(report_id, target_id, url)
        )
    else:
        text = f"Unsupported Mastodon webhook event: {event_name}"
        keyboard = None

    return text, keyboard


def _object_type_for_event(event_name: str) -> str | None:
    if event_name.startswith("account."):
        return "account"
    if event_name.startswith("report."):
        return "report"
    return None


def _should_send_new_message(event_name: str, obj: dict[str, Any]) -> bool:
    if event_name == "report.created":
        return True
    if event_name in {"account.created", "account.approved", "account.updated"}:
        return obj.get("confirmed") is True
    return False


def _is_message_not_modified(exc: TelegramAPIError) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in exc.message.lower()


def _webhook_lock_key(object_type: str, object_id: str, chat_id: int) -> str:
    return f"webhook:{object_type}:{object_id}:{chat_id}"
