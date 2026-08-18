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
from mastodon_admin_bot.storage.models import ModerationOperation, PendingAccount
from mastodon_admin_bot.storage.repository import Repository
from mastodon_admin_bot.telegram.keyboards import (
    applied_block_actions,
    open_keyboard,
    post_rejection_keyboard,
)
from mastodon_admin_bot.telegram.render import render_account_event

logger = logging.getLogger(__name__)


async def auto_reject_due_accounts(
    *,
    repository: Repository,
    bot: Bot,
    mastodon_origin: str,
    default_reject_after_seconds: int,
    trusted_telegram_user_ids: set[int] | None = None,
) -> int:
    now = datetime.now(UTC)
    trusted_ids = trusted_telegram_user_ids or set()
    await reconcile_uncertain_operations(
        repository=repository,
        bot=bot,
        mastodon_origin=mastodon_origin,
        trusted_telegram_user_ids=trusted_ids,
    )
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
            trusted_telegram_user_ids=trusted_ids,
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
    trusted_telegram_user_ids: set[int] | None = None,
) -> bool:
    creator_id = pending.matched_rule_created_by
    trusted_ids = trusted_telegram_user_ids or set()
    if creator_id is None or creator_id not in trusted_ids:
        logger.warning(
            "Auto-reject rule creator is not currently trusted",
            extra={"account_id": pending.account_id, "creator_id": creator_id},
        )
        return False
    token_data = await repository.get_moderator_token(creator_id)
    if token_data is None:
        logger.warning(
            "No linked moderator token for auto-reject",
            extra={"account_id": pending.account_id},
        )
        return False
    token, username = token_data
    operation_key = f"account_decision:{pending.account_id}"
    claim = await repository.claim_moderation_operation(
        operation_key=operation_key,
        action="rn",
        object_type="account",
        object_id=pending.account_id,
        target_id=None,
        requested_by=creator_id,
        handled_by=f"auto ({username})",
    )
    if claim != "claimed":
        return False
    try:
        async with MastodonClient(mastodon_origin, token=token) as client:
            await client.reject_account(pending.account_id)
    except MastodonApiError as exc:
        if exc.status_code == 429:
            await repository.fail_moderation_operation(
                operation_key,
                error=f"Mastodon HTTP 429: {exc.message}",
                retry_after=await _retry_delay(repository, operation_key),
            )
            return False
        elif exc.status_code >= 500 or exc.status_code in {403, 404, 422}:
            await repository.fail_moderation_operation(
                operation_key,
                error=f"Mastodon HTTP {exc.status_code}: {exc.message}",
                uncertain=True,
            )
            return False
        else:
            await repository.fail_moderation_operation(
                operation_key,
                error=f"Mastodon HTTP {exc.status_code}: {exc.message}",
                retry_after=timedelta(minutes=5),
            )
            logger.warning(
                "Auto-reject failed with a client error",
                extra={
                    "account_id": pending.account_id,
                    "status": exc.status_code,
                    "error": exc.message,
                },
            )
            return False
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        await repository.fail_moderation_operation(
            operation_key,
            error=f"transport or response error: {type(exc).__name__}",
            uncertain=True,
        )
        logger.warning(
            "Auto-reject network error; will retry",
            extra={"account_id": pending.account_id},
        )
        return False

    await repository.complete_moderation_operation(
        operation_key,
        pending_state="auto_rejected",
        handled_by=f"auto ({username})",
    )
    await _update_messages_after_auto_reject(
        bot=bot,
        repository=repository,
        pending=pending,
        username=username,
        rejected_account=_rejected_account_from_snapshot(pending),
    )
    return True


async def reconcile_uncertain_operations(
    *,
    repository: Repository,
    bot: Bot,
    mastodon_origin: str,
    trusted_telegram_user_ids: set[int],
) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=5)
    operations = await repository.list_uncertain_moderation_operations(older_than=cutoff)
    reconciled = 0
    for operation in operations:
        if (
            operation.requested_by is None
            or operation.requested_by not in trusted_telegram_user_ids
        ):
            continue
        token_data = await repository.get_moderator_token(operation.requested_by)
        if token_data is None:
            continue
        token, username = token_data
        try:
            async with MastodonClient(mastodon_origin, token=token) as client:
                result = await _read_operation_target(client, operation.action, operation)
        except MastodonApiError as exc:
            if exc.status_code == 404 and operation.action in {"an", "rn"}:
                await repository.complete_moderation_operation(
                    operation.operation_key,
                    pending_state="auto_rejected" if operation.action == "rn" else "rejected",
                    handled_by=operation.handled_by or username,
                )
                await _update_messages_after_reconciliation(
                    bot,
                    repository,
                    mastodon_origin,
                    operation.object_type,
                    operation.object_id,
                    operation.action,
                )
                reconciled += 1
            elif exc.status_code == 404:
                await repository.complete_moderation_operation(
                    operation.operation_key,
                    pending_state=(
                        "missing_external" if operation.action in {"ao", "af"} else None
                    ),
                    handled_by="external Mastodon state",
                    operation_status="conflict",
                )
                await _update_messages_after_reconciliation(
                    bot,
                    repository,
                    mastodon_origin,
                    operation.object_type,
                    operation.object_id,
                    operation.action,
                )
                reconciled += 1
            continue
        except (httpx.HTTPError, ValueError, KeyError):
            continue
        outcome = _operation_outcome(operation.action, result)
        if outcome == "succeeded":
            pending_state = _reconciled_pending_state(operation.action)
            await repository.complete_moderation_operation(
                operation.operation_key,
                pending_state=pending_state,
                handled_by=operation.handled_by or username,
            )
            await _update_messages_after_reconciliation(
                bot,
                repository,
                mastodon_origin,
                operation.object_type,
                operation.object_id,
                operation.action,
            )
            reconciled += 1
        elif outcome == "conflict":
            await repository.complete_moderation_operation(
                operation.operation_key,
                pending_state=(
                    "approved_external" if result.get("approved") is True else None
                ),
                handled_by="external Mastodon decision",
                operation_status="conflict",
            )
            await _update_messages_after_reconciliation(
                bot,
                repository,
                mastodon_origin,
                operation.object_type,
                operation.object_id,
                operation.action,
            )
            reconciled += 1
        else:
            await repository.fail_moderation_operation(
                operation.operation_key,
                error="reconciliation found action not applied",
                retry_after=await _retry_delay(repository, operation.operation_key),
            )
    return reconciled


async def _read_operation_target(
    client: MastodonClient,
    action: str,
    operation: ModerationOperation,
) -> dict[str, Any]:
    if action == "rr":
        return await client.get_admin_report(operation.object_id)
    account_id = operation.target_id if action in {"al", "au"} else operation.object_id
    if account_id is None:
        raise ValueError("operation has no account target")
    return await client.get_admin_account(account_id)


def _operation_outcome(action: str, result: dict[str, Any]) -> str:
    if action in {"ao", "af"}:
        return "succeeded" if result.get("approved") is True else "pending"
    if action in {"an", "rn"}:
        return "conflict" if result.get("approved") is True else "pending"
    if action == "rr":
        return "succeeded" if result.get("action_taken") is True else "pending"
    if action == "al":
        return "succeeded" if result.get("silenced") is True else "pending"
    if action == "au":
        return "succeeded" if result.get("suspended") is True else "pending"
    return "conflict"


def _reconciled_pending_state(action: str) -> str | None:
    if action in {"ao", "af"}:
        return "approved"
    if action in {"an", "rn"}:
        return "auto_rejected" if action == "rn" else "rejected"
    return None


async def _retry_delay(repository: Repository, operation_key: str) -> timedelta:
    operation = await repository.get_moderation_operation(operation_key)
    attempts = operation.attempts if operation is not None else 1
    return timedelta(seconds=min(30 * (2 ** (attempts - 1)), 3600))


async def _update_messages_after_reconciliation(
    bot: Bot,
    repository: Repository,
    mastodon_origin: str,
    object_type: str,
    object_id: str,
    action: str,
) -> None:
    if object_type == "account" and action in {"an", "rn"}:
        pending = await repository.get_pending_account(object_id)
        snapshot = snapshot_from_json(pending.account_snapshot) if pending is not None else {}
        include_reason = bool(snapshot.get("reason"))
        keyboard = post_rejection_keyboard(
            object_id,
            include_reason=include_reason,
            exclude=await applied_block_actions(repository, snapshot),
        )
    else:
        page = "accounts" if object_type == "account" else "reports"
        keyboard = open_keyboard(f"{mastodon_origin.rstrip('/')}/admin/{page}/{object_id}")
    for mapping in await repository.get_message_mappings(
        object_type=object_type, object_id=object_id
    ):
        try:
            await bot.edit_message_reply_markup(
                chat_id=mapping.chat_id,
                message_id=mapping.message_id,
                reply_markup=keyboard,
            )
        except TelegramAPIError as exc:
            if not _is_message_not_modified(exc):
                logger.warning(
                    "Failed to update reconciled moderation message",
                    extra={"chat_id": mapping.chat_id, "message_id": mapping.message_id},
                )


def _rejected_account_from_snapshot(pending: PendingAccount) -> dict[str, Any]:
    # The Mastodon reject API response does not reliably include the account's
    # display fields, so render the updated message from the stored snapshot.
    snapshot = snapshot_from_json(pending.account_snapshot)
    account: dict[str, Any] = {}
    acct = snapshot.get("acct", "")
    if acct:
        account = {"acct": acct}
    return {
        "id": pending.account_id,
        "approved": False,
        "email": snapshot.get("email", ""),
        "ip": snapshot.get("ip", ""),
        "locale": snapshot.get("locale", ""),
        "account": account,
        "invite_request": snapshot.get("reason", ""),
    }


async def _update_messages_after_auto_reject(
    *,
    bot: Bot,
    repository: Repository,
    pending: PendingAccount,
    username: str,
    rejected_account: dict[str, Any] | None,
) -> None:
    snapshot = snapshot_from_json(pending.account_snapshot)
    include_reason = bool(snapshot.get("reason"))
    keyboard = post_rejection_keyboard(
        pending.account_id,
        include_reason=include_reason,
        exclude=await applied_block_actions(repository, snapshot),
    )
    suffix = f"\n\nAuto-rejected by bot ({escape(username)})"
    mappings = await repository.get_message_mappings(
        object_type="account", object_id=pending.account_id
    )
    for mapping in mappings:
        try:
            if rejected_account is not None:
                text = render_account_event("account.rejected", rejected_account) + suffix
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
