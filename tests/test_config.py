from typing import Any

import pytest

from mastodon_admin_bot.config import Settings

REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": "123:token",
    "MASTODON_BASE_URL": "https://mastodon.example",
    "MASTODON_WEBHOOK_SECRET": "webhook-secret",
    "MASTODON_CLIENT_ID": "client-id",
    "MASTODON_CLIENT_SECRET": "client-secret",
    "MASTODON_REDIRECT_URI": "https://bot.example/oauth/callback",
    "TOKEN_ENCRYPTION_KEY": "a" * 44,
}


def make_settings(**overrides: object) -> Settings:
    values: dict[str, Any] = dict(REQUIRED_ENV)
    values.update(overrides)
    return Settings(**values)


def test_telegram_id_sets_are_parsed_independently() -> None:
    settings = make_settings(
        TRUSTED_TELEGRAM_USER_IDS="1001,1002",
        TELEGRAM_HOME_CHAT_IDS="-2001, -2002",
    )

    assert settings.trusted_telegram_user_ids == {1001, 1002}
    assert settings.telegram_home_chat_ids == {-2001, -2002}


@pytest.mark.parametrize(
    ("trusted_ids", "home_chat_ids", "expected_trusted", "expected_home"),
    [
        ("773343726", "-2001", {773343726}, {-2001}),
        ("1001,1002", "-2001, -2002", {1001, 1002}, {-2001, -2002}),
    ],
)
def test_telegram_id_sets_are_parsed_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    trusted_ids: str,
    home_chat_ids: str,
    expected_trusted: set[int],
    expected_home: set[int],
) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TRUSTED_TELEGRAM_USER_IDS", trusted_ids)
    monkeypatch.setenv("TELEGRAM_HOME_CHAT_IDS", home_chat_ids)

    settings = Settings()

    assert settings.trusted_telegram_user_ids == expected_trusted
    assert settings.telegram_home_chat_ids == expected_home


def test_data_retention_defaults_and_validates_positive_days() -> None:
    assert make_settings().data_retention_days == 30
    assert make_settings(DATA_RETENTION_DAYS=90).data_retention_days == 90
    with pytest.raises(ValueError):
        make_settings(DATA_RETENTION_DAYS=0)


def test_ip_lookup_prefix_lengths_default_and_validate_ranges() -> None:
    settings = make_settings()
    assert settings.ip_lookup_ipv4_prefix_length == 24
    assert settings.ip_lookup_ipv6_prefix_length == 80

    configured = make_settings(
        IP_LOOKUP_IPV4_PREFIX_LENGTH=20,
        IP_LOOKUP_IPV6_PREFIX_LENGTH=64,
    )
    assert configured.ip_lookup_ipv4_prefix_length == 20
    assert configured.ip_lookup_ipv6_prefix_length == 64

    with pytest.raises(ValueError):
        make_settings(IP_LOOKUP_IPV4_PREFIX_LENGTH=33)
    with pytest.raises(ValueError):
        make_settings(IP_LOOKUP_IPV6_PREFIX_LENGTH=129)
