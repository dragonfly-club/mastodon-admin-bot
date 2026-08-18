from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from aiohttp import web

from mastodon_admin_bot.app_state import (
    AUTOBAN_SWEEPER_TASK_KEY,
    MAINTENANCE_TASK_KEY,
    POLLING_TASK_KEY,
)
from mastodon_admin_bot.autoban import (
    AutobanInfo,
    find_match,
    render_auto_reject_at_line,
    render_match_line,
    snapshot_account,
    snapshot_to_json,
)
from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.ipinfo import IpInfoLookup
from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.mastodon.client import MastodonApiError, MastodonClient
from mastodon_admin_bot.mastodon.webhooks import (
    MastodonWebhook,
    is_pending_local_account,
    parse_webhook_payload,
)
from mastodon_admin_bot.security import verify_mastodon_signature
from mastodon_admin_bot.storage.repository import Repository
from mastodon_admin_bot.telegram.keyboards import (
    account_keyboard,
    autoban_keyboard,
    open_keyboard,
    report_keyboard,
)
from mastodon_admin_bot.telegram.render import (
    render_account_event,
    render_report_event,
)

_WEBHOOK_LOCKS = KeyedAsyncLocks()
logger = logging.getLogger(__name__)


def build_routes(
    settings: Settings,
    repository: Repository,
    bot: Bot,
    ip_client: IpInfoLookup | None = None,
    webhook_locks: KeyedAsyncLocks = _WEBHOOK_LOCKS,
) -> web.RouteTableDef:
    routes = web.RouteTableDef()

    @routes.get("/healthz")
    async def healthz(request: web.Request) -> web.Response:
        components: dict[str, bool] = {}
        try:
            await repository.check_database()
            components["database"] = True
        except Exception:
            components["database"] = False
        task_keys = (
            ("polling", POLLING_TASK_KEY),
            ("autoban_sweeper", AUTOBAN_SWEEPER_TASK_KEY),
            ("maintenance", MAINTENANCE_TASK_KEY),
        )
        for name, key in task_keys:
            task = request.app.get(key)
            components[name] = task is not None and not task.done()
        healthy = all(components.values())
        return web.json_response(
            {"ok": healthy, "components": components},
            status=200 if healthy else 503,
        )

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
        object_id = event.object_id
        if object_type is None or object_id is None:
            return web.json_response({"ok": True, "ignored": True})

        ip_geo = await _lookup_ip_geo(repository, ip_client, event)
        autoban = await _maybe_build_autoban_info(
            repository, event, object_id, settings, ip_geo=ip_geo
        )

        failed_chat_ids: list[int] = []
        if settings.telegram_home_chat_ids:
            for chat_id in settings.telegram_home_chat_ids:
                failed = await _deliver_event_to_chat(
                    repository=repository,
                    bot=bot,
                    webhook_locks=webhook_locks,
                    chat_id=chat_id,
                    object_type=object_type,
                    object_id=object_id,
                    event_name=event.event,
                    obj=event.object,
                    mastodon_origin=settings.mastodon_origin,
                    autoban=autoban,
                    ip_geo=ip_geo,
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

        telegram_user_id = await repository.claim_oauth_state(state)
        if telegram_user_id is None:
            return web.Response(text="Invalid, expired, or already active OAuth state", status=400)

        try:
            async with MastodonClient(settings.mastodon_origin) as client:
                token_response = await client.exchange_oauth_code(
                    code=code,
                    client_id=settings.mastodon_client_id.get_secret_value(),
                    client_secret=settings.mastodon_client_secret.get_secret_value(),
                    redirect_uri=str(settings.mastodon_redirect_uri),
                )
                access_token = str(token_response["access_token"])
                scopes = str(token_response.get("scope") or "")
            if not _scopes_cover(settings.mastodon_scopes, scopes):
                raise ValueError("Mastodon granted fewer scopes than requested")
            async with MastodonClient(settings.mastodon_origin, token=access_token) as user_client:
                credentials = await user_client.verify_credentials()
                await user_client.verify_admin_access()
            account_id = credentials.get("id")
            if account_id is None:
                raise ValueError("Mastodon credentials response has no account ID")
            mastodon_account_id = str(account_id)
            mastodon_username = str(
                credentials.get("acct") or credentials.get("username") or mastodon_account_id
            )
            stored = await repository.store_moderator_link_and_consume_state(
                state=state,
                telegram_user_id=telegram_user_id,
                mastodon_account_id=mastodon_account_id,
                mastodon_username=mastodon_username,
                access_token=access_token,
                scopes=scopes,
            )
            if not stored:
                return web.Response(text="OAuth state changed; run /link again", status=409)
        except (httpx.HTTPError, MastodonApiError, KeyError, ValueError):
            await repository.release_oauth_state(state)
            logger.warning("OAuth linking failed", extra={"telegram_user_id": telegram_user_id})
            return web.Response(text="Mastodon linking failed; retry or run /link", status=502)
        return web.Response(
            text=f"Linked Mastodon account {mastodon_username}. You can close this page."
        )

    return routes


async def _lookup_ip_geo(
    repository: Repository,
    ip_client: IpInfoLookup | None,
    event: MastodonWebhook,
) -> str:
    """Best-effort geolocation text for pending registrations; '' when not available."""
    if ip_client is None or not await repository.get_ip_lookup_enabled():
        return ""
    if event.event != "account.created" or not is_pending_local_account(event.object):
        return ""
    raw_ip = event.object.get("ip")
    if not isinstance(raw_ip, str) or not raw_ip.strip():
        return ""
    try:
        info = await ip_client.lookup(raw_ip)
    except Exception:
        logger.warning("IP lookup failed unexpectedly", exc_info=True)
        return ""
    return info.location_text()


async def _maybe_build_autoban_info(
    repository: Repository,
    event: Any,
    object_id: str,
    settings: Settings,
    ip_geo: str = "",
) -> AutobanInfo | None:
    if event.event != "account.created" or not is_pending_local_account(event.object):
        return None
    snapshot = snapshot_account(event.object, ip_geo)
    match = await find_match(repository, event.object)
    matched_rule_type: str | None = None
    matched_pattern: str | None = None
    matched_rule_created_by: int | None = None
    if match is not None:
        matched_rule_type = match.rule_type
        matched_pattern = match.pattern
        matched_rule_created_by = match.created_by
    pending = await repository.upsert_pending_account(
        account_id=object_id,
        account_snapshot=snapshot_to_json(snapshot),
        matched_rule_type=matched_rule_type,
        matched_pattern=matched_pattern,
        matched_rule_created_by=matched_rule_created_by,
    )
    if pending.matched_rule_type is None:
        return AutobanInfo()
    notify_enabled = await repository.get_notify_blocked_users_enabled()
    timeout_seconds = await repository.get_autoban_timeout_seconds(
        settings.autoban_default_reject_after_seconds
    )
    return AutobanInfo(
        matched_rule_type=pending.matched_rule_type,
        matched_pattern=pending.matched_pattern,
        auto_reject_at=_as_utc(pending.webhook_received_at) + timedelta(seconds=timeout_seconds),
        silent=not notify_enabled,
    )


async def _send_event_message(
    *,
    bot: Bot,
    chat_id: int,
    event_name: str,
    obj: dict[str, Any],
    mastodon_origin: str,
    autoban: AutobanInfo | None = None,
    ip_geo: str = "",
) -> Any:
    text, keyboard = _render_event_message(event_name, obj, mastodon_origin, autoban, ip_geo)
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
        disable_notification=autoban is not None and autoban.silent,
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
    autoban: AutobanInfo | None = None,
    ip_geo: str = "",
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
                if await _account_already_handled(repository, object_type, object_id):
                    # No live message for this object and the account was already
                    # decided: do not (re)send a stale notification with buttons.
                    return False
                message = await _send_event_message(
                    bot=bot,
                    chat_id=chat_id,
                    event_name=event_name,
                    obj=obj,
                    mastodon_origin=mastodon_origin,
                    autoban=autoban,
                    ip_geo=ip_geo,
                )
                await repository.upsert_message_mapping(
                    object_type=object_type,
                    object_id=object_id,
                    chat_id=chat_id,
                    message_id=message.message_id,
                )
            else:
                try:
                    await _edit_event_message(
                        bot=bot,
                        chat_id=chat_id,
                        message_id=mapping.message_id,
                        event_name=event_name,
                        obj=obj,
                        mastodon_origin=mastodon_origin,
                        autoban=autoban,
                        ip_geo=ip_geo,
                    )
                except TelegramAPIError as exc:
                    if not _is_message_not_found(exc):
                        raise
                    # The mapped message is gone: drop the stale mapping and, if
                    # this event is still worth notifying about, send a fresh one.
                    await repository.delete_message_mapping(
                        object_type=object_type,
                        object_id=object_id,
                        chat_id=chat_id,
                    )
                    if not _should_send_new_message(event_name, obj):
                        return False
                    if await _account_already_handled(repository, object_type, object_id):
                        return False
                    message = await _send_event_message(
                        bot=bot,
                        chat_id=chat_id,
                        event_name=event_name,
                        obj=obj,
                        mastodon_origin=mastodon_origin,
                        autoban=autoban,
                        ip_geo=ip_geo,
                    )
                    await repository.upsert_message_mapping(
                        object_type=object_type,
                        object_id=object_id,
                        chat_id=chat_id,
                        message_id=message.message_id,
                    )
    except TelegramAPIError as exc:
        if _is_message_not_modified(exc):
            return False
        # Keep the existing mapping; a later webhook can retry the edit.
        return True
    return False


async def _account_already_handled(
    repository: Repository,
    object_type: str,
    object_id: str,
) -> bool:
    if object_type != "account":
        return False
    pending = await repository.get_pending_account(object_id)
    return pending is not None and pending.state != "pending"


def _is_message_not_found(exc: TelegramAPIError) -> bool:
    return (
        isinstance(exc, TelegramBadRequest)
        and "message to edit not found" in exc.message.lower()
    )


async def _edit_event_message(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    event_name: str,
    obj: dict[str, Any],
    mastodon_origin: str,
    autoban: AutobanInfo | None = None,
    ip_geo: str = "",
) -> Any:
    text, keyboard = _render_event_message(event_name, obj, mastodon_origin, autoban, ip_geo)
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
    autoban: AutobanInfo | None = None,
    ip_geo: str = "",
) -> tuple[str, InlineKeyboardMarkup | None]:
    if event_name == "account.created":
        account_id = str(obj.get("id"))
        url = f"{mastodon_origin}/admin/accounts/{account_id}" if account_id else None
        auto_banned = (
            autoban is not None
            and autoban.matched_rule_type is not None
            and autoban.matched_pattern is not None
            and autoban.auto_reject_at is not None
        )
        text = render_account_event(event_name, obj, auto_banned=auto_banned, ip_geo=ip_geo)
        if is_pending_local_account(obj):
            keyboard: InlineKeyboardMarkup | None
            if (
                autoban is not None
                and autoban.matched_rule_type is not None
                and autoban.matched_pattern is not None
                and autoban.auto_reject_at is not None
            ):
                text = (
                    text
                    + "\n"
                    + render_match_line(autoban.matched_rule_type, autoban.matched_pattern)
                )
                text = text + "\n" + render_auto_reject_at_line(autoban.auto_reject_at)
                keyboard = autoban_keyboard(account_id)
            else:
                keyboard = account_keyboard(account_id, url)
        else:
            keyboard = None
    elif event_name == "report.created":
        text = render_report_event(obj)
        report_id = str(obj.get("id"))
        target = obj.get("target_account")
        target_id = str(target.get("id")) if isinstance(target, dict) and target.get("id") else None
        url = f"{mastodon_origin}/admin/reports/{report_id}"
        keyboard = (
            open_keyboard(url)
            if obj.get("action_taken") is True
            else report_keyboard(report_id, target_id, url)
        )
    else:
        text = f"Unsupported Mastodon webhook event: {event_name}"
        keyboard = None

    return text, keyboard


def _object_type_for_event(event_name: str) -> str | None:
    if event_name == "account.created":
        return "account"
    if event_name == "report.created":
        return "report"
    return None


def _should_send_new_message(event_name: str, obj: dict[str, Any]) -> bool:
    if event_name == "report.created":
        return True
    if event_name == "account.created":
        return obj.get("approved") is not True
    return False


def _is_message_not_modified(exc: TelegramAPIError) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in exc.message.lower()


def _webhook_lock_key(object_type: str, object_id: str, chat_id: int) -> str:
    return f"webhook:{object_type}:{object_id}:{chat_id}"


def _scopes_cover(requested: str, granted: str) -> bool:
    return set(requested.split()).issubset(granted.split())


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
