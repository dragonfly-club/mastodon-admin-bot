# Mastodon Admin Telegram Bot

Telegram bot for forwarding Mastodon admin webhooks to trusted moderators and applying moderation actions back to Mastodon.

The bot uses per-moderator Mastodon OAuth tokens for write actions. That means Mastodon records the actual moderator in its audit fields instead of a shared bot account.

## Setup

```bash
uv sync
cp .env.example .env
uv run mastodon-admin-bot
```

Register a Mastodon OAuth application with the scopes shown in `.env.example`, then configure a Mastodon admin webhook pointing at:

```text
https://your-public-host/mastodon/webhook
```

Webhook messages are sent only to the chat IDs listed in `TELEGRAM_HOME_CHAT_IDS`.
Moderation commands and callback buttons are restricted to the Telegram user IDs
listed in `TRUSTED_TELEGRAM_USER_IDS`.

Telegram updates are received via polling. With the default Telegram Bot API service,
`TELEGRAM_BOT_TOKEN` is enough for the bot to receive updates. To use a custom
Telegram Bot API server, set:

```text
TELEGRAM_API_BASE_URL=http://localhost:8081
```

## Development

```bash
uv run pytest
uv run ruff check
uv run mypy src tests
```

Database schema changes are managed with Alembic. The application upgrades the
configured database on startup; for a manual upgrade, run:

```bash
uv run alembic upgrade head
```

Webhook messages are deduplicated by Mastodon object ID: report webhooks by
report ID, and account webhooks by account ID. The bot sends new Telegram
messages only for `report.created` and pending local `account.created` events.
Other webhook events, including `report.updated`, `account.updated`, and
`account.approved`, are ignored. Moderation actions taken from Telegram update
the clicked message; matching messages in other configured chats have their
moderation buttons replaced with an Open button when there is still a useful
admin page to inspect. Rejected account registrations are marked handled without
an Open button.

## Docker

```bash
docker build -t mastodon-admin-bot .
docker volume create mastodon-admin-bot-data
docker run \
  --env-file .env \
  -v mastodon-admin-bot-data:/data \
  -p 8080:8080 \
  mastodon-admin-bot
```

The image listens on `BIND_PORT`, defaulting to `8080`, and binds to `0.0.0.0` inside the container.
