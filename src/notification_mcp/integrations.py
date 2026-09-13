"""Generate standard MCP settings and register this server in Codex."""

import json
import os
import sys
import tomllib
from pathlib import Path

from notification_mcp.config import Settings
from notification_mcp.installation import atomic_write
from notification_mcp.worker_lock import WorkerLock


def server_url(settings: Settings) -> str:
    host = settings.server.host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.server.port}"


def client_entry(settings: Settings, config: Path, transport: str) -> dict:
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


def client_config(settings: Settings, config: Path, client: str, transport: str = "http") -> str:
    entry = client_entry(settings, config, transport)
    if client == "codex":
        return (
            "[mcp_servers.notifications]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in entry.items())
            + "\n"
        )
    return json.dumps({"mcpServers": {"notifications": entry}}, ensure_ascii=False, indent=2)


def _matches_entry(existing: object, expected: dict) -> bool:
    if not isinstance(existing, dict) or not existing.get("enabled", True):
        return False
    transport_keys = {"url", "command", "args"}
    return all(existing.get(key) == value for key, value in expected.items()) and not any(
        key in existing and key not in expected for key in transport_keys
    )


def connect_codex(settings: Settings, config: Path, transport: str = "http") -> tuple[Path, bool]:
    directory = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    target = directory.expanduser().resolve() / "config.toml"
    expected = client_entry(settings, config, transport)
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
        updated = text.rstrip() + "\n\n" + client_config(settings, config, "codex", transport)
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
