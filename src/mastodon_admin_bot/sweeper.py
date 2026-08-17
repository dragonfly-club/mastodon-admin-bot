from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

import httpx
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from mastodon_admin_bot.autoban import snapshot_from_json
from mastodon_admin_bot.mastodon.client import MastodonApiError, MastodonClient
from mastodon_admin_bot.storage.models import PendingAccount
from mastodon_admin_bot.storage.repository import Repository
from mastodon_admin_bot.telegram.keyboards import post_rejection_keyboard
from mastodon_admin_bot.telegram.render import render_account_event

logger = logging.getLogger(__name__)


async def auto_reject_due_accounts(
    *,
    repository: Repository,
    bot: Bot,
    mastodon_origin: str,
    default_reject_after_seconds: int,
) -> int:
    now = datetime.now(UTC)
    timeout_seconds = await repository.get_autoban_timeout_seconds(
        default_reject_after_seconds
    )
    due = await repository.list_due_pending_auto_bans(
        now - timedelta(seconds=timeout_seconds)
    )
    processed = 0
    for pending in due:
        handled = await _auto_reject_one(
            pending=pending,
            repository=repository,
            bot=bot,
            mastodon_origin=mastodon_origin,
        )
        if handled:
            processed += 1
    return processed


async def _auto_reject_one(
    *,
    pending: PendingAccount,
    repository: Repository,
    bot: Bot,
    mastodon_origin: str,
) -> bool:
    token_data = await repository.get_linked_moderator_token(pending.matched_rule_created_by)
    if token_data is None:
        logger.warning(
            "No linked moderator token for auto-reject",
            extra={"account_id": pending.account_id},
        )
        return False
    token, username = token_data
    api_result: dict[str, Any] | None
    try:
        async with MastodonClient(mastodon_origin, token=token) as client:
            api_result = await client.reject_account(pending.account_id)
    except MastodonApiError as exc:
        if exc.status_code in (404, 422):
            await repository.mark_pending_account_handled(
                account_id=pending.account_id,
                state="auto_rejected",
                handled_by=f"auto ({username})",
            )
            await _update_messages_after_auto_reject(
                bot=bot,
                repository=repository,
                pending=pending,
                username=username,
                api_result=None,
            )
        else:
            await repository.mark_pending_account_handled(
                account_id=pending.account_id,
                state="rejected_error",
                handled_by=f"auto ({username})",
            )
            logger.warning(
                "Auto-reject gave up on Mastodon API error",
                extra={
                    "account_id": pending.account_id,
                    "status": exc.status_code,
                    "error": exc.message,
                },
            )
        return True
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning(
            "Auto-reject network error; will retry",
            extra={"account_id": pending.account_id},
        )
        return False

    await repository.mark_pending_account_handled(
        account_id=pending.account_id,
        state="auto_rejected",
        handled_by=f"auto ({username})",
    )
    await _update_messages_after_auto_reject(
        bot=bot,
        repository=repository,
        pending=pending,
        username=username,
        api_result=api_result,
    )
    return True


async def _update_messages_after_auto_reject(
    *,
    bot: Bot,
    repository: Repository,
    pending: PendingAccount,
    username: str,
    api_result: dict[str, Any] | None,
) -> None:
    snapshot = snapshot_from_json(pending.account_snapshot)
    include_reason = bool(snapshot.get("reason"))
    keyboard = post_rejection_keyboard(pending.account_id, include_reason=include_reason)
    suffix = f"\n\nAuto-rejected by bot ({escape(username)})"
    mappings = await repository.get_message_mappings(
        object_type="account", object_id=pending.account_id
    )
    for mapping in mappings:
        try:
            if api_result is not None:
                text = render_account_event("account.rejected", api_result) + suffix
                await bot.edit_message_text(
                    chat_id=mapping.chat_id,
                    message_id=mapping.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            else:
                await bot.edit_message_reply_markup(
                    chat_id=mapping.chat_id,
                    message_id=mapping.message_id,
                    reply_markup=keyboard,
                )
        except TelegramAPIError as exc:
            if not _is_message_not_modified(exc):
                logger.warning(
                    "Failed to update auto-rejected message",
                    extra={
                        "chat_id": mapping.chat_id,
                        "message_id": mapping.message_id,
                        "error": exc.message,
                    },
                )


def _is_message_not_modified(exc: TelegramAPIError) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in exc.message.lower()
