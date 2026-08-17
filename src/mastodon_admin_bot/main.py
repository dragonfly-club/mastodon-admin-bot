from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress
from typing import Any, cast

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

from mastodon_admin_bot.config import get_settings
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.migrations import upgrade_database
from mastodon_admin_bot.storage.repository import Repository, create_engine
from mastodon_admin_bot.sweeper import auto_reject_due_accounts
from mastodon_admin_bot.telegram.handlers import build_router
from mastodon_admin_bot.web.routes import build_routes

logger = logging.getLogger(__name__)
_SWEEPER_INTERVAL_SECONDS = 15


def build_bot() -> Bot:
    settings = get_settings()
    session = None
    if settings.telegram_api_base_url:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(str(settings.telegram_api_base_url)),
        )
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_app() -> web.Application:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = Repository.from_engine(
        engine,
        TokenCipher.from_key(settings.token_encryption_key.get_secret_value()),
    )
    bot = build_bot()
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(settings, repository))

    app = web.Application()
    app["engine"] = engine
    app["repository"] = repository
    app["bot"] = bot

    async def init_db(_app: web.Application) -> None:
        await upgrade_database(settings.database_url)
        await repository.purge_oauth_states()

    async def start_polling(_app: web.Application) -> None:
        await bot.delete_webhook(drop_pending_updates=False)
        await _register_bot_commands(bot)
        _app["polling_task"] = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                allowed_updates=dispatcher.resolve_used_update_types(),
            ),
        )

    async def start_autoban_sweeper(_app: web.Application) -> None:
        async def _loop() -> None:
            while True:
                try:
                    await auto_reject_due_accounts(
                        repository=repository,
                        bot=bot,
                        mastodon_origin=settings.mastodon_origin,
                        default_reject_after_seconds=(
                            settings.autoban_default_reject_after_seconds
                        ),
                    )
                except Exception:
                    logger.exception("Autoban sweeper iteration failed")
                await asyncio.sleep(_SWEEPER_INTERVAL_SECONDS)

        _app["autoban_sweeper_task"] = asyncio.create_task(_loop())

    async def stop_polling(_app: web.Application) -> None:
        task = cast(asyncio.Task[Any], _app["polling_task"])
        if not task.done():
            await dispatcher.stop_polling()
        with suppress(asyncio.CancelledError):
            await task

    async def stop_autoban_sweeper(_app: web.Application) -> None:
        task = cast(asyncio.Task[Any], _app["autoban_sweeper_task"])
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def dispose(_app: web.Application) -> None:
        await engine.dispose()

    app.on_startup.append(init_db)
    app.on_startup.append(start_polling)
    app.on_startup.append(start_autoban_sweeper)
    app.on_cleanup.append(stop_polling)
    app.on_cleanup.append(stop_autoban_sweeper)
    app.on_cleanup.append(dispose)
    app.add_routes(build_routes(settings, repository, bot))
    return app


async def _register_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Show bot help"),
        BotCommand(command="link", description="Link your Mastodon moderator account"),
        BotCommand(command="whoami", description="Show linked Mastodon account"),
        BotCommand(
            command="blockemail", description="Add an email regex to the blocklist"
        ),
        BotCommand(
            command="blockemaildomain",
            description="Add an email-domain regex to the blocklist",
        ),
        BotCommand(
            command="blockreason", description="Add an invite-reason regex to the blocklist"
        ),
        BotCommand(command="unblockemail", description="Remove an email regex"),
        BotCommand(
            command="unblockemaildomain", description="Remove an email-domain regex"
        ),
        BotCommand(command="unblockreason", description="Remove an invite-reason regex"),
        BotCommand(command="blocklist", description="List all blocklist rules"),
        BotCommand(
            command="autobantimeout",
            description="Show or set the auto-reject timeout (seconds)",
        ),
        BotCommand(
            command="notifyblockeduser",
            description="Show or set notifications for auto-blocked accounts (on|off)",
        ),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception:
        logger.exception("Failed to register bot commands")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    settings = get_settings()
    web.run_app(create_app(), host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
