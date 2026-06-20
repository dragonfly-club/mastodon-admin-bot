from typing import Any

from mastodon_admin_bot.config import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": "123:token",
        "MASTODON_BASE_URL": "https://mastodon.example",
        "MASTODON_WEBHOOK_SECRET": "webhook-secret",
        "MASTODON_CLIENT_ID": "client-id",
        "MASTODON_CLIENT_SECRET": "client-secret",
        "MASTODON_REDIRECT_URI": "https://bot.example/oauth/callback",
        "TOKEN_ENCRYPTION_KEY": "a" * 44,
    }
    values.update(overrides)
    return Settings(**values)


def test_telegram_id_sets_are_parsed_independently() -> None:
    settings = make_settings(
        TRUSTED_TELEGRAM_USER_IDS="1001,1002",
        TELEGRAM_HOME_CHAT_IDS="-2001, -2002",
    )

    assert settings.trusted_telegram_user_ids == {1001, 1002}
    assert settings.telegram_home_chat_ids == {-2001, -2002}
