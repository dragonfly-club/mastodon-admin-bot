FROM python:3.14-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim-trixie AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BIND_HOST=0.0.0.0 \
    BIND_PORT=8080 \
    DATABASE_URL=sqlite+aiosqlite:////data/bot.db

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

EXPOSE 8080

CMD ["mastodon-admin-bot"]
