from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncEngine

from mastodon_admin_bot.storage.repository import Repository

ENGINE_KEY = web.AppKey("engine", AsyncEngine)
REPOSITORY_KEY = web.AppKey("repository", Repository)
BOT_KEY = web.AppKey("bot", Bot)
POLLING_TASK_KEY = web.AppKey("polling_task", asyncio.Task[Any])
AUTOBAN_SWEEPER_TASK_KEY = web.AppKey("autoban_sweeper_task", asyncio.Task[Any])
MAINTENANCE_TASK_KEY = web.AppKey("maintenance_task", asyncio.Task[Any])
