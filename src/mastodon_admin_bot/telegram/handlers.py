from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from html import escape
from typing import Any
from urllib.parse import urlencode

import httpx
import regex
from aiogram import Bot, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message
from aiogram.utils.markdown import hcode

from mastodon_admin_bot.autoban import (
    RULE_TYPE_EMAIL,
    RULE_TYPE_EMAIL_DOMAIN,
    RULE_TYPE_REASON,
    compile_rule_pattern,
    snapshot_from_json,
)
from mastodon_admin_bot.config import Settings
from mastodon_admin_bot.locks import KeyedAsyncLocks
from mastodon_admin_bot.mastodon.client import MastodonApiError, MastodonClient
from mastodon_admin_bot.security import make_state
from mastodon_admin_bot.storage.models import BlocklistRule, PendingAccount
from mastodon_admin_bot.storage.repository import Repository

from .keyboards import (
    Action,
    AdminCallback,
    open_keyboard,
    post_rejection_keyboard,
)
from .render import render_account_event, render_report_event

logger = logging.getLogger(__name__)
_ACTION_LOCKS = KeyedAsyncLocks()

_ACCOUNT_DECISION_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.APPROVE_ACCOUNT,
        Action.REJECT_ACCOUNT,
        Action.FORCE_APPROVE_ACCOUNT,
        Action.REJECT_NOW_ACCOUNT,
    }
)
_BLOCK_ACTIONS: frozenset[Action] = frozenset(
    {Action.BLOCK_EMAIL, Action.BLOCK_EMAIL_DOMAIN, Action.BLOCK_REASON}
)
_BLOCK_ACTION_TO_RULE_TYPE: dict[Action, str] = {
    Action.BLOCK_EMAIL: RULE_TYPE_EMAIL,
    Action.BLOCK_EMAIL_DOMAIN: RULE_TYPE_EMAIL_DOMAIN,
    Action.BLOCK_REASON: RULE_TYPE_REASON,
}
_BLOCK_ACTION_TO_SNAPSHOT_FIELD: dict[Action, str] = {
    Action.BLOCK_EMAIL: "email",
    Action.BLOCK_EMAIL_DOMAIN: "email_domain",
    Action.BLOCK_REASON: "reason",
}


def build_router(
    settings: Settings,
    repository: Repository,
    action_locks: KeyedAsyncLocks = _ACTION_LOCKS,
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
        user_id = await _gate_management_command(message)
        if user_id is None:
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

    def _trusted_user_id(message: Message) -> int | None:
        user_id = message.from_user.id if message.from_user else None
        if not is_trusted_user(user_id) or user_id is None:
            return None
        return user_id

    async def _gate_management_command(message: Message) -> int | None:
        user_id = _trusted_user_id(message)
        if user_id is None:
            await message.answer("This bot is restricted to trusted moderators.")
            return None
        return user_id

    @router.message(Command("blockemail"))
    async def blockemail(message: Message, command: CommandObject) -> None:
        user_id = await _gate_management_command(message)
        if user_id is None:
            return
        await _add_blocklist_command(message, command, repository, RULE_TYPE_EMAIL, user_id)

    @router.message(Command("blockemaildomain"))
    async def blockemaildomain(message: Message, command: CommandObject) -> None:
        user_id = await _gate_management_command(message)
        if user_id is None:
            return
        await _add_blocklist_command(
            message, command, repository, RULE_TYPE_EMAIL_DOMAIN, user_id
        )

    @router.message(Command("blockreason"))
    async def blockreason(message: Message, command: CommandObject) -> None:
        user_id = await _gate_management_command(message)
        if user_id is None:
            return
        await _add_blocklist_command(message, command, repository, RULE_TYPE_REASON, user_id)

    @router.message(Command("unblockemail"))
    async def unblockemail(message: Message, command: CommandObject) -> None:
        if await _gate_management_command(message) is None:
            return
        await _remove_blocklist_command(message, command, repository, RULE_TYPE_EMAIL)

    @router.message(Command("unblockemaildomain"))
    async def unblockemaildomain(message: Message, command: CommandObject) -> None:
        if await _gate_management_command(message) is None:
            return
        await _remove_blocklist_command(
            message, command, repository, RULE_TYPE_EMAIL_DOMAIN
        )

    @router.message(Command("unblockreason"))
    async def unblockreason(message: Message, command: CommandObject) -> None:
        if await _gate_management_command(message) is None:
            return
        await _remove_blocklist_command(message, command, repository, RULE_TYPE_REASON)

    @router.message(Command("blocklist"))
    async def blocklist(message: Message) -> None:
        if await _gate_management_command(message) is None:
            return
        rules = await repository.list_blocklist_rules()
        if not rules:
            await message.answer("No blocklist rules.")
            return
        await message.answer(_render_blocklist(rules), parse_mode=ParseMode.HTML)

    @router.message(Command("autobantimeout"))
    async def autobantimeout(message: Message, command: CommandObject) -> None:
        if await _gate_management_command(message) is None:
            return
        arg = (command.args or "").strip()
        if not arg:
            current = await repository.get_autoban_timeout_seconds(
                settings.autoban_default_reject_after_seconds
            )
            await message.answer(
                f"Auto-reject timeout: {current}s ({_format_seconds(current)})."
            )
            return
        try:
            seconds = int(arg)
        except ValueError:
            await message.answer("Timeout must be an integer number of seconds.")
            return
        if seconds < 60:
            await message.answer("Timeout must be at least 60 seconds.")
            return
        await repository.set_autoban_timeout_seconds(seconds)
        await message.answer(
            f"Auto-reject timeout set to {seconds}s ({_format_seconds(seconds)})."
        )

    @router.message(Command("notifyblockeduser"))
    async def notifyblockeduser(message: Message, command: CommandObject) -> None:
        if await _gate_management_command(message) is None:
            return
        arg = (command.args or "").strip().lower()
        if not arg:
            current = await repository.get_notify_blocked_users_enabled()
            state = "on" if current else "off (default)"
            await message.answer(f"Notifications for auto-blocked accounts: {state}.")
            return
        if arg not in {"on", "off"}:
            await message.answer("Usage: /notifyblockeduser on|off")
            return
        await repository.set_notify_blocked_users_enabled(arg == "on")
        await message.answer(f"Notifications for auto-blocked accounts: {arg}.")

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

        if callback_data.action in _BLOCK_ACTIONS:
            await _handle_block_callback(
                query=query,
                callback_data=callback_data,
                repository=repository,
                bot=bot,
                action_locks=action_locks,
            )
            return

        token_data = await repository.get_moderator_token(user_id)
        if token_data is None:
            await query.answer("Run /link first.", show_alert=True)
            return
        token, mastodon_username = token_data

        async def execute_action() -> dict[str, Any]:
            async with MastodonClient(settings.mastodon_origin, token=token) as client:
                return await _execute_action(client, callback_data)

        error_message, api_result = await _run_durable_action_result(
            repository=repository,
            locks=action_locks,
            callback_data=callback_data,
            requested_by=user_id,
            handled_by=mastodon_username,
            execute=execute_action,
        )
        if error_message is not None:
            await query.answer(error_message, show_alert=True)
            return

        await query.answer("Done.")
        if query.message and not isinstance(query.message, InaccessibleMessage):
            current_text = query.message.html_text or query.message.text or ""
            pending = await repository.get_pending_account(callback_data.object_id)
            include_reason = _snapshot_has_reason(pending)
            reply_markup = _post_action_markup(
                settings.mastodon_origin, callback_data, include_reason
            )
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
                reply_markup=reply_markup,
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
                        reply_markup=reply_markup,
                    )
                except TelegramAPIError:
                    logger.warning(
                        "Failed to remove moderation keyboard",
                        extra={"chat_id": mapping.chat_id, "message_id": mapping.message_id},
                    )

    async def _handle_block_callback(
        *,
        query: CallbackQuery,
        callback_data: AdminCallback,
        repository: Repository,
        bot: Bot,
        action_locks: KeyedAsyncLocks,
    ) -> None:
        rule_type = _BLOCK_ACTION_TO_RULE_TYPE[callback_data.action]
        snapshot_field = _BLOCK_ACTION_TO_SNAPSHOT_FIELD[callback_data.action]
        user_id = query.from_user.id if query.from_user else None

        pending = await repository.get_pending_account(callback_data.object_id)
        if pending is None:
            await query.answer("Account snapshot not found.", show_alert=True)
            return
        snapshot = snapshot_from_json(pending.account_snapshot)
        value = snapshot.get(snapshot_field, "")
        if not value:
            await query.answer(
                f"{snapshot_field} not available for this account.", show_alert=True
            )
            return
        pattern = "^" + regex.escape(value) + "$"

        async def execute() -> dict[str, Any]:
            rule, _created = await repository.add_blocklist_rule(
                rule_type=rule_type,
                pattern=pattern,
                created_by=user_id,
            )
            return {"rule_id": rule.id, "pattern": rule.pattern}

        lock = await action_locks.try_acquire(_action_lock_key(callback_data))
        if lock is None:
            await query.answer("That rule is already being added.", show_alert=True)
            return
        async with lock:
            error_message, _result = await _run_action_result(execute)
        if error_message is not None:
            await query.answer(error_message, show_alert=True)
            return

        await query.answer(f"Added {rule_type} rule.")
        if query.message and not isinstance(query.message, InaccessibleMessage):
            include_reason = bool(snapshot.get("reason"))
            new_markup = post_rejection_keyboard(
                callback_data.object_id,
                include_reason=include_reason,
                exclude=callback_data.action,
            )
            try:
                await bot.edit_message_reply_markup(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    reply_markup=new_markup,
                )
            except TelegramAPIError as exc:
                if not _is_message_not_modified(exc):
                    logger.warning(
                        "Failed to refresh blocklist keyboard",
                        extra={
                            "chat_id": query.message.chat.id,
                            "message_id": query.message.message_id,
                        },
                    )

    async def _execute_action(
        client: MastodonClient,
        callback_data: AdminCallback,
    ) -> dict[str, Any]:
        match callback_data.action:
            case Action.APPROVE_ACCOUNT | Action.FORCE_APPROVE_ACCOUNT:
                return await client.approve_account(callback_data.object_id)
            case Action.REJECT_ACCOUNT | Action.REJECT_NOW_ACCOUNT:
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
            case _:
                raise MastodonApiError(400, f"unsupported action: {callback_data.action}")

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


async def _run_durable_action_result(
    *,
    repository: Repository,
    locks: KeyedAsyncLocks,
    callback_data: AdminCallback,
    requested_by: int,
    handled_by: str,
    execute: Callable[[], Awaitable[Any]],
) -> tuple[str | None, Any | None]:
    operation_key = _action_lock_key(callback_data)
    lock = await locks.try_acquire(operation_key)
    if lock is None:
        return "That moderation decision is already being handled.", None
    async with lock:
        claim = await repository.claim_moderation_operation(
            operation_key=operation_key,
            action=callback_data.action.value,
            object_type=_callback_mapping_object_type(callback_data),
            object_id=callback_data.object_id,
            target_id=callback_data.target_id,
            requested_by=requested_by,
            handled_by=handled_by,
        )
        if claim == "done":
            return "That moderation decision was already handled.", None
        if claim != "claimed":
            return "That moderation decision is already being handled.", None
        try:
            result = await execute()
        except MastodonApiError as exc:
            uncertain = exc.status_code >= 500 or (
                callback_data.action in _ACCOUNT_DECISION_ACTIONS
                and exc.status_code in {403, 404, 422}
            )
            retry_after = None
            if exc.status_code == 429:
                operation = await repository.get_moderation_operation(operation_key)
                attempts = operation.attempts if operation is not None else 1
                retry_after = timedelta(seconds=min(30 * (2 ** (attempts - 1)), 3600))
            await repository.fail_moderation_operation(
                operation_key,
                error=f"Mastodon HTTP {exc.status_code}: {exc.message}",
                uncertain=uncertain,
                retry_after=retry_after,
            )
            if uncertain:
                return "Mastodon returned an uncertain result; the bot will reconcile it.", None
            return f"Mastodon rejected action: {exc.message}", None
        except httpx.HTTPError as exc:
            await repository.fail_moderation_operation(
                operation_key,
                error=f"transport error: {type(exc).__name__}",
                uncertain=True,
            )
            return "Mastodon action outcome is uncertain; the bot will reconcile it.", None
        except (ValueError, KeyError) as exc:
            await repository.fail_moderation_operation(
                operation_key,
                error=f"invalid response: {type(exc).__name__}",
            )
            return "Mastodon action failed. Please retry.", None
        except Exception as exc:
            logger.exception("Unexpected moderation action failure")
            await repository.fail_moderation_operation(
                operation_key,
                error=f"unexpected error: {type(exc).__name__}",
                uncertain=True,
            )
            return "Unexpected moderation action failure; reconciliation is pending.", None
        try:
            pending_state = (
                _pending_state_for_action(callback_data.action)
                if callback_data.action in _ACCOUNT_DECISION_ACTIONS
                else None
            )
            await repository.complete_moderation_operation(
                operation_key,
                pending_state=pending_state,
                handled_by=handled_by,
            )
        except Exception as exc:
            logger.exception("Failed to persist successful moderation action")
            try:
                await repository.fail_moderation_operation(
                    operation_key,
                    error=f"persistence error: {type(exc).__name__}",
                    uncertain=True,
                )
            except Exception:
                logger.exception("Failed to persist uncertain moderation state")
            return "Action succeeded but state synchronization is pending.", None
        return None, result


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
        case Action.APPROVE_ACCOUNT | Action.FORCE_APPROVE_ACCOUNT:
            return "account.approved"
        case Action.REJECT_ACCOUNT | Action.REJECT_NOW_ACCOUNT:
            return "account.rejected"
        case _:
            raise ValueError(f"unsupported account action: {action}")


def _pending_state_for_action(action: Action) -> str:
    match action:
        case Action.APPROVE_ACCOUNT:
            return "approved"
        case Action.FORCE_APPROVE_ACCOUNT:
            return "force_approved"
        case Action.REJECT_ACCOUNT | Action.REJECT_NOW_ACCOUNT:
            return "rejected"
        case _:
            raise ValueError(f"unsupported account action: {action}")


def _open_markup(
    mastodon_origin: str,
    callback_data: AdminCallback,
) -> InlineKeyboardMarkup | None:
    if callback_data.action in (Action.REJECT_ACCOUNT, Action.REJECT_NOW_ACCOUNT):
        return None
    return open_keyboard(_open_url(mastodon_origin, callback_data))


def _post_action_markup(
    mastodon_origin: str,
    callback_data: AdminCallback,
    include_reason: bool,
) -> InlineKeyboardMarkup | None:
    if callback_data.action in (Action.REJECT_ACCOUNT, Action.REJECT_NOW_ACCOUNT):
        return post_rejection_keyboard(
            callback_data.object_id, include_reason=include_reason
        )
    return _open_markup(mastodon_origin, callback_data)


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
        case Action.FORCE_APPROVE_ACCOUNT:
            return "Force approved account"
        case Action.REJECT_ACCOUNT | Action.REJECT_NOW_ACCOUNT:
            return "Rejected account"
        case Action.RESOLVE_REPORT:
            return "Resolved report"
        case Action.LIMIT_TARGET:
            return "Limited target account"
        case Action.SUSPEND_TARGET:
            return "Suspended target account"
        case Action.BLOCK_EMAIL | Action.BLOCK_EMAIL_DOMAIN | Action.BLOCK_REASON:
            return "Added blocklist rule"


def _callback_mapping_object_type(callback_data: AdminCallback) -> str:
    match callback_data.action:
        case (
            Action.APPROVE_ACCOUNT
            | Action.REJECT_ACCOUNT
            | Action.FORCE_APPROVE_ACCOUNT
            | Action.REJECT_NOW_ACCOUNT
            | Action.BLOCK_EMAIL
            | Action.BLOCK_EMAIL_DOMAIN
            | Action.BLOCK_REASON
        ):
            return "account"
        case Action.RESOLVE_REPORT | Action.LIMIT_TARGET | Action.SUSPEND_TARGET:
            return "report"


def _action_lock_key(callback_data: AdminCallback) -> str:
    match callback_data.action:
        case (
            Action.APPROVE_ACCOUNT
            | Action.REJECT_ACCOUNT
            | Action.FORCE_APPROVE_ACCOUNT
            | Action.REJECT_NOW_ACCOUNT
        ):
            return f"account_decision:{callback_data.object_id}"
        case Action.RESOLVE_REPORT:
            return f"report_state:{callback_data.object_id}"
        case Action.LIMIT_TARGET | Action.SUSPEND_TARGET:
            target_id = callback_data.target_id or "unknown"
            return f"report_target_action:{callback_data.object_id}:{target_id}"
        case Action.BLOCK_EMAIL | Action.BLOCK_EMAIL_DOMAIN | Action.BLOCK_REASON:
            return f"block_add:{callback_data.object_id}:{callback_data.action.value}"


_COMMAND_FOR_RULE_TYPE: dict[str, str] = {
    RULE_TYPE_EMAIL: "blockemail",
    RULE_TYPE_EMAIL_DOMAIN: "blockemaildomain",
    RULE_TYPE_REASON: "blockreason",
}

_UNBLOCK_COMMAND_FOR_RULE_TYPE: dict[str, str] = {
    RULE_TYPE_EMAIL: "unblockemail",
    RULE_TYPE_EMAIL_DOMAIN: "unblockemaildomain",
    RULE_TYPE_REASON: "unblockreason",
}


def _snapshot_has_reason(pending: PendingAccount | None) -> bool:
    if pending is None:
        return False
    snapshot = snapshot_from_json(pending.account_snapshot)
    return bool(snapshot.get("reason"))


def _format_seconds(seconds: int) -> str:
    if seconds >= 3600:
        hours, remainder = divmod(seconds, 3600)
        if remainder:
            return f"{hours}h {remainder // 60}m"
        return f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _render_blocklist(rules: list[BlocklistRule]) -> str:
    by_type: dict[str, list[str]] = {}
    for rule in rules:
        by_type.setdefault(rule.rule_type, []).append(rule.pattern)
    lines: list[str] = []
    for rule_type in (RULE_TYPE_EMAIL, RULE_TYPE_EMAIL_DOMAIN, RULE_TYPE_REASON):
        patterns = by_type.get(rule_type)
        if not patterns:
            continue
        lines.append(f"<b>{escape(rule_type)}</b> ({len(patterns)}):")
        for pattern in patterns:
            lines.append(f"  {hcode(pattern)}")
    return "\n".join(lines)


async def _add_blocklist_command(
    message: Message,
    command: CommandObject,
    repository: Repository,
    rule_type: str,
    user_id: int,
) -> None:
    command_name = _COMMAND_FOR_RULE_TYPE.get(rule_type, "block")
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(f"Usage: /{command_name} &lt;regex&gt;")
        return
    try:
        compile_rule_pattern(arg)
    except regex.error as exc:
        await message.answer(f"Invalid regex: {escape(str(exc))}")
        return
    _rule, created = await repository.add_blocklist_rule(
        rule_type=rule_type, pattern=arg, created_by=user_id
    )
    if created:
        await message.answer(f"Added {escape(rule_type)} rule: {escape(arg)}")
    else:
        await message.answer(
            f"Rule already exists: {escape(rule_type)} {escape(arg)}"
        )


async def _remove_blocklist_command(
    message: Message,
    command: CommandObject,
    repository: Repository,
    rule_type: str,
) -> None:
    command_name = _UNBLOCK_COMMAND_FOR_RULE_TYPE.get(rule_type, "unblock")
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(f"Usage: /{command_name} &lt;regex&gt;")
        return
    removed = await repository.remove_blocklist_rule(rule_type=rule_type, pattern=arg)
    if removed:
        await message.answer(f"Removed {removed} {escape(rule_type)} rule(s).")
    else:
        await message.answer(f"No matching {escape(rule_type)} rule.")
