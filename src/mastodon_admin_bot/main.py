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
from aiohttp import web

from mastodon_admin_bot.config import get_settings
from mastodon_admin_bot.security import TokenCipher
from mastodon_admin_bot.storage.migrations import upgrade_database
from mastodon_admin_bot.storage.repository import Repository, create_engine
from mastodon_admin_bot.telegram.handlers import build_router
from mastodon_admin_bot.web.routes import build_routes


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
        _app["polling_task"] = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                allowed_updates=dispatcher.resolve_used_update_types(),
            ),
        )

    async def stop_polling(_app: web.Application) -> None:
        task = cast(asyncio.Task[Any], _app["polling_task"])
        if not task.done():
            await dispatcher.stop_polling()
        with suppress(asyncio.CancelledError):
            await task

    async def dispose(_app: web.Application) -> None:
        await engine.dispose()

    app.on_startup.append(init_db)
    app.on_startup.append(start_polling)
    app.on_cleanup.append(stop_polling)
    app.on_cleanup.append(dispose)
    app.add_routes(build_routes(settings, repository, bot))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    settings = get_settings()
    web.run_app(create_app(), host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
