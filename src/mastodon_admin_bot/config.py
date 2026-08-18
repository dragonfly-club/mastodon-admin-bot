from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_int_set(value: str | set[int]) -> set[int]:
    if isinstance(value, set):
        return value
    if not value.strip():
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_api_base_url: AnyHttpUrl | None = Field(
        default=None,
        alias="TELEGRAM_API_BASE_URL",
    )
    bind_host: str = Field(default="127.0.0.1", alias="BIND_HOST")
    bind_port: int = Field(default=8080, alias="BIND_PORT")

    mastodon_base_url: AnyHttpUrl = Field(alias="MASTODON_BASE_URL")
    mastodon_webhook_secret: SecretStr = Field(alias="MASTODON_WEBHOOK_SECRET")
    mastodon_client_id: SecretStr = Field(alias="MASTODON_CLIENT_ID")
    mastodon_client_secret: SecretStr = Field(alias="MASTODON_CLIENT_SECRET")
    mastodon_redirect_uri: AnyHttpUrl = Field(alias="MASTODON_REDIRECT_URI")
    mastodon_scopes: str = Field(
        default=(
            "profile admin:read:accounts admin:write:accounts "
            "admin:read:reports admin:write:reports"
        ),
        alias="MASTODON_SCOPES",
    )

    trusted_telegram_user_ids: Annotated[set[int], NoDecode] = Field(
        default_factory=set,
        alias="TRUSTED_TELEGRAM_USER_IDS",
    )
    telegram_home_chat_ids: Annotated[set[int], NoDecode] = Field(
        default_factory=set,
        alias="TELEGRAM_HOME_CHAT_IDS",
    )
    database_url: str = Field(default="sqlite+aiosqlite:///./bot.db", alias="DATABASE_URL")
    token_encryption_key: SecretStr = Field(alias="TOKEN_ENCRYPTION_KEY")
    autoban_default_reject_after_seconds: int = Field(
        default=43200,
        ge=60,
        alias="AUTOBAN_DEFAULT_REJECT_AFTER_SECONDS",
    )
    data_retention_days: int = Field(default=30, ge=1, alias="DATA_RETENTION_DAYS")
    ip_lookup_ipv4_prefix_length: int = Field(
        default=24,
        ge=0,
        le=32,
        alias="IP_LOOKUP_IPV4_PREFIX_LENGTH",
    )
    ip_lookup_ipv6_prefix_length: int = Field(
        default=80,
        ge=0,
        le=128,
        alias="IP_LOOKUP_IPV6_PREFIX_LENGTH",
    )

    @field_validator(
        "trusted_telegram_user_ids",
        "telegram_home_chat_ids",
        mode="before",
    )
    @classmethod
    def parse_int_set(cls, value: str | set[int]) -> set[int]:
        return _parse_int_set(value)

    @property
    def mastodon_origin(self) -> str:
        return str(self.mastodon_base_url).rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
