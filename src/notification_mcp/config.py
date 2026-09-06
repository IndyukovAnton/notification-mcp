import os
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from notification_mcp.models import Event


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(ConfigModel):
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class StorageConfig(ConfigModel):
    database: Path = Path("var/notifications.sqlite3")


class DeliveryConfig(ConfigModel):
    poll_interval_seconds: float = Field(default=1, ge=0.05, le=60, allow_inf_nan=False)
    request_timeout_seconds: float = Field(default=10, ge=0.1, le=60, allow_inf_nan=False)
    max_attempts: int = Field(default=8, ge=1, le=100)
    retry_base_seconds: float = Field(default=5, ge=0.1, le=3600, allow_inf_nan=False)
    retry_max_seconds: float = Field(default=300, ge=0.1, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_retry_range(self) -> "DeliveryConfig":
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        return self


class TelegramChannel(ConfigModel):
    type: Literal["telegram"] = "telegram"
    bot_token: SecretStr | None = Field(default=None, exclude=True)
    bot_token_env: str = Field(default="TELEGRAM_BOT_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    chat_id: str | None = Field(default=None, pattern=r"^-?[1-9][0-9]*$")

    @field_validator("bot_token")
    @classmethod
    def validate_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[1-9][0-9]*:[A-Za-z0-9_-]+", value.get_secret_value()
        ):
            raise ValueError("bot_token must be a Telegram bot token from BotFather")
        return value

    def token(self) -> str:
        if self.bot_token is not None:
            return self.bot_token.get_secret_value()
        value = os.environ.get(self.bot_token_env, "").strip()
        if not value:
            raise ValueError(
                f"Set bot_token in config or environment variable {self.bot_token_env}"
            )
        if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]+", value):
            raise ValueError("Invalid Telegram token format; use the token from BotFather")
        return value


class TelegramDestination(ConfigModel):
    """Persisted recipient and credential reference, never the credential itself."""

    type: Literal["telegram"] = "telegram"
    chat_id: str = Field(pattern=r"^-?[1-9][0-9]*$")
    bot_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    # Compatibility with notifications queued by the environment-only version.
    bot_token_env: str = Field(default="TELEGRAM_BOT_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class FileChannel(ConfigModel):
    type: Literal["file"] = "file"
    path: Path


Channel = Annotated[TelegramChannel | FileChannel, Field(discriminator="type")]
Destination = Annotated[TelegramDestination | FileChannel, Field(discriminator="type")]


class RoutingConfig(ConfigModel):
    default_channel: str
    events: dict[Event, str] = Field(default_factory=dict)


class Settings(ConfigModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    routing: RoutingConfig
    channels: dict[str, Channel] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_routes(self) -> "Settings":
        for name in self.channels:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError("Channel names must contain 1-64 letters, digits, '_' or '-'")
        for name in [self.routing.default_channel, *self.routing.events.values()]:
            if name not in self.channels:
                raise ValueError(f"Routing references unknown channel: {name}")
        return self

    def destination(self, event: Event) -> tuple[str, Channel]:
        name = self.routing.events.get(event, self.routing.default_channel)
        return name, self.channels[name]


def load_settings(path: Path, *, check_secrets: bool = True) -> Settings:
    path = path.expanduser().resolve()
    with path.open("rb") as stream:
        settings = Settings.model_validate(tomllib.load(stream))

    def absolute(value: Path) -> Path:
        return (path.parent / value.expanduser()).resolve()

    settings = settings.model_copy(
        update={
            "storage": settings.storage.model_copy(
                update={"database": absolute(settings.storage.database)}
            ),
            "channels": {
                name: channel.model_copy(update={"path": absolute(channel.path)})
                if isinstance(channel, FileChannel)
                else channel
                for name, channel in settings.channels.items()
            },
        }
    )
    automatic_bots = set()
    for channel in settings.channels.values():
        if isinstance(channel, FileChannel) and channel.path == settings.storage.database:
            raise ValueError("File channel must not write to the queue database")
        if check_secrets and isinstance(channel, TelegramChannel):
            bot_id = channel.token().split(":", 1)[0]
            if channel.chat_id is None:
                if bot_id in automatic_bots:
                    raise ValueError("Only one automatic Telegram channel per bot is supported")
                automatic_bots.add(bot_id)
    return settings
