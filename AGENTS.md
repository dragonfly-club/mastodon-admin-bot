# AGENTS.md

Notes for OpenCode sessions. See `README.md` for product/setup context and `.env.example` for required env vars.

## Commands

Toolchain is `uv` (not pip). Run everything via `uv run`.

```bash
uv sync                         # install deps + dev group into .venv
uv run pytest                   # tests
uv run ruff check               # lint (NO `ruff format` is configured — only `check`)
uv run mypy src tests           # typecheck (strict)
uv run mastodon-admin-bot       # run app; entry point src/mastodon_admin_bot/main.py:main
uv run alembic upgrade head     # manual DB migration (rarely needed — app auto-upgrades on startup)
```

Focused test: `uv run pytest tests/test_repository.py::test_message_mapping_round_trip`.

There is **no CI for tests, lint, or typecheck** — the only workflow builds the container image on `v*` tags. Local runs are the only gate; run `ruff check` and `mypy src tests` before considering work done.

## Architecture

- One process runs two things concurrently: an aiohttp HTTP server (`create_app` in `main.py`) and a Telegram long-polling task started on app startup. Don't assume just one.
- HTTP routes: `POST /mastodon/webhook`, `GET /oauth/callback`, `GET /healthz`. Telegram updates are polled (a custom Bot API server is optional via `TELEGRAM_API_BASE_URL`).
- `web/routes.py` holds a module-level `_WEBHOOK_LOCKS` keyed by `object_type:object_id:chat_id` to dedupe concurrent webhooks for the same object.
- Source root is `src/mastodon_admin_bot/` with `config`, `security`, `locks`, `storage/` (models, repository, migrations), `mastodon/` (client, webhooks), `telegram/` (handlers, keyboards, render), `web/` (routes).

## Config & env gotchas

- `Settings` (`config.py`, pydantic-settings, reads `.env`) is returned by an `@lru_cache`d `get_settings()` — env changes after first load don't apply. In tests, build `Settings(**values)` directly (see `tests/test_config.py`).
- `TRUSTED_TELEGRAM_USER_IDS` / `TELEGRAM_HOME_CHAT_IDS` are comma-separated strings parsed into `set[int]` via `NoDecode` + a `before` validator. Don't type them as `int`.
- `TOKEN_ENCRYPTION_KEY` must be a valid Fernet key; moderator OAuth tokens are encrypted at rest with it.

## Database & migrations

- Default DB is `sqlite+aiosqlite:///./bot.db` (or `/data/bot.db` in Docker); override via `DATABASE_URL`.
- App runs `alembic upgrade head` automatically on startup (`init_db` in `main.py`).
- `migrations/env.py` is async and imports `Base.metadata` from `storage/models.py`. Changing a model requires a matching Alembic revision — there is no autogenerate step wired up. Migration files use `YYYYMMDD_NNNN_description.py` names with matching `revision` IDs.
- The migration history was squashed into one initial revision; keep the live migration schema in sync with `Base.metadata`.

## Tests

- No `conftest.py`; tests are self-contained and need **no `.env`** (they build `Repository`/`Settings` directly).
- Tests use in-memory SQLite (`sqlite+aiosqlite:///:memory:`) and mostly call `repo.create_schema(engine)` = `Base.metadata.create_all`, NOT Alembic. Only `test_migrations_create_usable_schema` runs real migrations — run it specifically when you change models.
- No network: Telegram is faked (`FakeBot`), Mastodon HTTP is faked. Each async test owns and disposes its own engine.
- `pytest` uses `asyncio_mode = "auto"`: write `async def test_...` without `@pytest.mark.asyncio`.

## Style & typecheck

- `mypy` is `strict` with `warn_unreachable = true` and the `pydantic.mypy` plugin — dead branches, `Any`, or missing annotations fail. Don't suppress casually.
- Runtime is effectively Python 3.14 (Dockerfile + mypy `python_version`), despite `requires-python = ">=3.12"`.
