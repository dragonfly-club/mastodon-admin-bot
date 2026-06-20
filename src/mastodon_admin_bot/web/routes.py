from __future__ import annotations

import json
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.mastodon.client import MastodonClient
from mastodon_admin_bot.mastodon.webhooks import is_pending_local_account, parse_webhook_payload
from mastodon_admin_bot.security import verify_mastodon_signature
from mastodon_admin_bot.storage.repository import Repository
from mastodon_admin_bot.telegram.keyboards import account_keyboard, report_keyboard
from mastodon_admin_bot.telegram.render import (
    render_account_event,
    render_report_event,
    render_status_event,
)


def build_routes(settings: Settings, repository: Repository, bot: Bot) -> web.RouteTableDef:
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

        stored, inserted = await repository.record_webhook_event(
            dedupe_key=event.dedupe_key,
            event_type=event.event,
            object_id=event.object_id,
            payload=payload,
            legacy_dedupe_key=event.legacy_dedupe_key,
        )

        failed_chat_ids: list[int] = []
        if settings.telegram_home_chat_ids:
            if inserted:
                chat_ids = settings.telegram_home_chat_ids
            else:
                pending_deliveries = await repository.get_pending_deliveries(stored.id)
                chat_ids = {delivery.chat_id for delivery in pending_deliveries}
            for chat_id in chat_ids:
                await repository.ensure_delivery(stored.id, chat_id)
                try:
                    message = await _send_event_message(
                        bot=bot,
                        chat_id=chat_id,
                        event_id=stored.id,
                        event_name=event.event,
                        obj=event.object,
                        mastodon_origin=settings.mastodon_origin,
                    )
                    await repository.mark_delivery_sent(stored.id, chat_id, message.message_id)
                except TelegramAPIError as exc:
                    failed_chat_ids.append(chat_id)
                    await repository.mark_delivery_failed(stored.id, chat_id, str(exc))
        status = 502 if failed_chat_ids else 200
        return web.json_response(
            {
                "ok": not failed_chat_ids,
                "duplicate": not inserted,
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
    event_id: int,
    event_name: str,
    obj: dict[str, Any],
    mastodon_origin: str,
) -> Any:
    if event_name.startswith("account."):
        text = render_account_event(event_name, obj)
        account_id = str(obj.get("id"))
        url = f"{mastodon_origin}/admin/accounts/{account_id}" if account_id else None
        keyboard = (
            account_keyboard(account_id, event_id, url) if is_pending_local_account(obj) else None
        )
    elif event_name.startswith("report."):
        text = render_report_event(obj)
        report_id = str(obj.get("id"))
        target = obj.get("target_account")
        target_id = str(target.get("id")) if isinstance(target, dict) and target.get("id") else None
        keyboard = report_keyboard(
            report_id,
            target_id,
            event_id,
            f"{mastodon_origin}/admin/reports/{report_id}",
        )
    elif event_name.startswith("status."):
        text = render_status_event(obj, event_name)
        keyboard = None
    else:
        text = f"Unsupported Mastodon webhook event: {event_name}"
        keyboard = None

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
