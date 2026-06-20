from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config


def _alembic_config(database_url: str) -> Config:
    project_root = Path.cwd()
    if not (project_root / "alembic.ini").exists():
        project_root = Path(__file__).resolve().parents[3]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def upgrade_database(database_url: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(database_url), "head")
