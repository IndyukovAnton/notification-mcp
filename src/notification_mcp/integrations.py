"""Generate standard MCP settings and register this server in Codex."""

import json
import os
import sys
import tomllib
from pathlib import Path

from notification_mcp.config import Settings, TelegramChannel
from notification_mcp.installation import atomic_write
from notification_mcp.worker_lock import WorkerLock

TOKEN_PLACEHOLDER = "<PASTE_TELEGRAM_BOT_TOKEN_HERE>"


def server_url(settings: Settings) -> str:
    host = settings.server.host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.server.port}"


def client_entry(
    settings: Settings | None,
    config: Path | None,
    transport: str,
    *,
    token_env: str | None = None,
    token_value: str | None = None,
) -> dict:
    if token_env is not None:
        if transport != "stdio":
            raise ValueError("Environment-token mode requires stdio transport")
        TelegramChannel(bot_token_env=token_env)
        return {
            "command": "notification-mcp",
            "env": {token_env: token_value or TOKEN_PLACEHOLDER},
        }
    if settings is None or config is None:
        raise ValueError("Settings and config are required outside environment-token mode")
    if transport == "http":
        return {"url": server_url(settings) + "/mcp"}
    return {
        "command": sys.executable,
        "args": [
            "-m",
            "notification_mcp",
            "--config",
            str(config),
            "serve",
            "--transport",
            "stdio",
        ],
    }


def client_config(
    settings: Settings | None,
    config: Path | None,
    client: str,
    transport: str = "http",
    *,
    token_env: str | None = None,
    token_value: str | None = None,
) -> str:
    entry = client_entry(
        settings,
        config,
        transport,
        token_env=token_env,
        token_value=token_value,
    )
    if client == "codex":
        env = entry.pop("env", None)
        result = "[mcp_servers.notifications]\n" + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in entry.items()
        )
        if env:
            result += "\n\n[mcp_servers.notifications.env]\n" + "\n".join(
                f"{key} = {json.dumps(value)}" for key, value in env.items()
            )
        return result + "\n"
    return json.dumps({"mcpServers": {"notifications": entry}}, ensure_ascii=False, indent=2)


def _matches_entry(existing: object, expected: dict) -> bool:
    if not isinstance(existing, dict) or not existing.get("enabled", True):
        return False
    transport_keys = {"url", "command", "args", "env", "env_vars"}
    return all(existing.get(key) == value for key, value in expected.items()) and not any(
        key in existing and key not in expected for key in transport_keys
    )


def connect_codex(
    settings: Settings | None,
    config: Path | None,
    transport: str = "http",
    *,
    token_env: str | None = None,
    token_value: str | None = None,
) -> tuple[Path, bool]:
    directory = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    target = directory.expanduser().resolve() / "config.toml"
    expected = client_entry(
        settings,
        config,
        transport,
        token_env=token_env,
        token_value=token_value,
    )
    with WorkerLock(target.with_name("notification-mcp-connect")):
        text = target.read_text(encoding="utf-8-sig") if target.exists() else ""
        data = tomllib.loads(text)
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("Codex mcp_servers must be a table; edit the Codex config first")
        existing = servers.get("notifications")
        if existing is not None:
            if _matches_entry(existing, expected):
                return target, False
            raise ValueError(
                "Codex already has different or disabled notifications settings. "
                f"Run 'codex mcp remove notifications', then retry. Config: {target}"
            )
        updated = (
            text.rstrip()
            + "\n\n"
            + client_config(
                settings,
                config,
                "codex",
                transport,
                token_env=token_env,
                token_value=token_value,
            )
        )
        try:
            parsed = tomllib.loads(updated)
        except tomllib.TOMLDecodeError:
            raise ValueError(
                "Cannot append MCP settings to this Codex config; use client-config"
            ) from None
        if not _matches_entry(parsed.get("mcp_servers", {}).get("notifications"), expected):
            raise ValueError("Cannot add MCP settings to this Codex config; use client-config")
        atomic_write(target, updated)
        return target, True
