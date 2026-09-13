"""Personal configuration and first-run setup, independent of the source checkout."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from notification_mcp.config import (
    DEFAULT_TELEGRAM_TOKEN_ENV,
    FileChannel,
    RoutingConfig,
    Settings,
    StorageConfig,
    TelegramChannel,
    load_settings,
)
from notification_mcp.worker_lock import WorkerLock


def personal_config() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "notification-mcp/config.toml"


def resolve_config(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    override = os.environ.get("NOTIFICATION_MCP_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    # Do not silently load credentials from an arbitrary current working directory.
    return personal_config().resolve()


def environment_settings(token_env: str = DEFAULT_TELEGRAM_TOKEN_ENV) -> Settings:
    """Build the single-Telegram-channel profile without reading or writing a config file."""
    channel = TelegramChannel(bot_token_env=token_env)
    channel.token()
    return Settings(
        storage=StorageConfig(database=personal_config().parent / "var/notifications.sqlite3"),
        routing=RoutingConfig(default_channel="personal"),
        channels={"personal": channel},
    )


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary_path.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def settings_toml(settings: Settings) -> str:
    values = settings.model_dump(mode="json")
    for name, channel in settings.channels.items():
        if isinstance(channel, TelegramChannel) and channel.bot_token is not None:
            values["channels"][name]["bot_token"] = channel.bot_token.get_secret_value()
    lines = ["# Local settings. Contains credentials; do not publish this file."]

    def table(data: dict, keys: tuple[str, ...] = ()) -> None:
        if keys:
            lines.extend(["", "[" + ".".join(json.dumps(key) for key in keys) + "]"])
        for key, value in data.items():
            if value is not None and not isinstance(value, dict):
                lines.append(f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}")
        for key, value in data.items():
            if isinstance(value, dict) and value:
                table(value, (*keys, key))

    table(values)
    return "\n".join(lines) + "\n"


def setup_config(
    path: Path,
    *,
    token: str | None = None,
    local: bool = False,
    import_config: Path | None = None,
) -> bool:
    if path.exists():
        load_settings(path)
        return False
    if import_config is not None:
        settings = load_settings(import_config)
        # Freeze environment credentials into the personal file for one-time configuration.
        channels = {
            name: TelegramChannel(bot_token=channel.token(), chat_id=channel.chat_id)
            if isinstance(channel, TelegramChannel)
            else channel
            for name, channel in settings.channels.items()
        }
        database = path.parent / "var/notifications.sqlite3"
        if settings.storage.database != database:
            if database.exists():
                raise ValueError(
                    "Target database already exists; choose another configuration path"
                )
            with WorkerLock(settings.storage.database):
                if settings.storage.database.exists():
                    database.parent.mkdir(parents=True, exist_ok=True)
                    source_uri = settings.storage.database.as_uri() + "?mode=ro"
                    source = sqlite3.connect(source_uri, uri=True)
                    target = sqlite3.connect(database)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                        source.close()
        settings = settings.model_copy(
            update={
                "channels": channels,
                "storage": StorageConfig(database=database),
            }
        )
    else:
        channel = (
            FileChannel(path=Path("var/notifications.jsonl"))
            if local
            else TelegramChannel(bot_token=token)
        )
        if isinstance(channel, TelegramChannel):
            channel.token()
        settings = Settings(
            routing=RoutingConfig(default_channel="personal"), channels={"personal": channel}
        )
    # Serialize setup of the same target, and never overwrite existing credentials.
    with WorkerLock(path.with_name(path.name + ".setup")):
        if path.exists():
            return False
        atomic_write(path, settings_toml(settings))
    return True
