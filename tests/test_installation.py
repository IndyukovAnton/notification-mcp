import json
import sys
import tomllib
from pathlib import Path

import pytest

from notification_mcp.cli import main, parser
from notification_mcp.config import DEFAULT_TELEGRAM_TOKEN_ENV, load_settings
from notification_mcp.installation import environment_settings, resolve_config, setup_config
from notification_mcp.integrations import client_config, connect_codex
from notification_mcp.models import Notification
from notification_mcp.service import NotificationService
from notification_mcp.store import Store
from notification_mcp.worker_lock import WorkerLock


def test_setup_saves_token_once_and_never_overwrites_existing_config(tmp_path):
    target = tmp_path / "personal/config.toml"
    assert setup_config(target, token="123:private-token")
    settings = load_settings(target)
    assert settings.channels["personal"].token() == "123:private-token"
    original = target.read_bytes()
    assert not setup_config(target, token="999:different-token")
    assert target.read_bytes() == original
    assert settings.storage.database == target.parent / "var/notifications.sqlite3"


def test_default_config_is_independent_of_current_directory(tmp_path, monkeypatch):
    personal = tmp_path / "personal.toml"
    monkeypatch.setenv("NOTIFICATION_MCP_CONFIG", str(personal))
    (tmp_path / "config.toml").write_text("untrusted current directory")
    monkeypatch.chdir(tmp_path)
    assert resolve_config(None) == personal
    explicit = tmp_path / "explicit.toml"
    assert resolve_config(explicit) == explicit


def test_environment_settings_use_forwarded_token_without_creating_config(tmp_path, monkeypatch):
    config = tmp_path / "local/notification-mcp/config.toml"
    monkeypatch.setattr("notification_mcp.installation.personal_config", lambda: config)
    monkeypatch.setenv(DEFAULT_TELEGRAM_TOKEN_ENV, "123:private-token")

    settings = environment_settings()

    assert settings.channels["personal"].token() == "123:private-token"
    assert settings.storage.database == config.parent / "var/notifications.sqlite3"
    assert not config.exists()


def test_import_keeps_queue_binding_and_token(tmp_path):
    source = tmp_path / "old/config.toml"
    setup_config(source, token="123:private-token")
    service = NotificationService(load_settings(source))
    service.store.initialize()
    service.store.bind_telegram("123", "42", "42", private=True, update_id=1)
    note = service.enqueue(Notification(message="queued before install"))
    target = tmp_path / "personal/config.toml"
    assert setup_config(target, import_config=source)
    imported = load_settings(target)
    store = Store(imported.storage.database)
    assert store.path != service.store.path
    assert store.status(note.notification.id).status == "queued"
    assert store.telegram_chat("123") == "42"
    assert imported.channels["personal"].token() == "123:private-token"


def test_import_refuses_running_worker_and_existing_database(tmp_path):
    source = tmp_path / "old/config.toml"
    setup_config(source, local=True)
    settings = load_settings(source)
    target = tmp_path / "personal/config.toml"
    with WorkerLock(settings.storage.database):
        with pytest.raises(RuntimeError, match="already using"):
            setup_config(target, import_config=source)
    assert not target.exists()
    database = target.parent / "var/notifications.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"existing data")
    with pytest.raises(ValueError, match="already exists"):
        setup_config(target, import_config=source)
    assert database.read_bytes() == b"existing data"


def test_codex_connect_preserves_settings_is_repeatable_and_hides_credentials(
    settings, tmp_path, monkeypatch
):
    codex_dir = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    codex_dir.mkdir()
    target = codex_dir / "config.toml"
    original = (
        '# Keep this comment\nmodel="test-model"\n[mcp_servers.docs]\nurl="http://localhost:99"\n'
    )
    target.write_text(original, encoding="utf-8")
    path, changed = connect_codex(settings, tmp_path / "config.toml", "http")
    assert changed and path == target
    text = target.read_text(encoding="utf-8")
    assert text.startswith(original)
    data = tomllib.loads(text)
    assert data["mcp_servers"]["notifications"]["url"] == "http://127.0.0.1:8765/mcp"
    assert data["model"] == "test-model"
    assert not connect_codex(settings, tmp_path / "config.toml", "http")[1]
    assert target.read_text(encoding="utf-8") == text


def test_codex_connect_supports_repeatable_stdio(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    config = tmp_path / "personal/config.toml"
    target, changed = connect_codex(settings, config, "stdio")
    assert changed
    entry = tomllib.loads(target.read_text(encoding="utf-8"))["mcp_servers"]["notifications"]
    assert Path(entry["command"]).is_absolute()
    assert entry["args"] == [
        "-m",
        "notification_mcp",
        "--config",
        str(config),
        "serve",
        "--transport",
        "stdio",
    ]
    assert "url" not in entry
    assert not connect_codex(settings, config, "stdio")[1]


def test_codex_connect_supports_config_free_environment_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    target, changed = connect_codex(None, None, "stdio", token_env=DEFAULT_TELEGRAM_TOKEN_ENV)

    assert changed
    entry = tomllib.loads(target.read_text(encoding="utf-8"))["mcp_servers"]["notifications"]
    assert Path(entry["command"]).is_absolute()
    assert entry["args"] == [
        "-m",
        "notification_mcp",
        "serve",
        "--transport",
        "stdio",
    ]
    assert entry["env_vars"] == [DEFAULT_TELEGRAM_TOKEN_ENV]
    assert "--config" not in entry["args"]
    assert not connect_codex(None, None, "stdio", token_env=DEFAULT_TELEGRAM_TOKEN_ENV)[1]


def test_environment_token_client_config_supports_custom_variable():
    data = tomllib.loads(client_config(None, None, "codex", "stdio", token_env="MY_TELEGRAM_TOKEN"))
    entry = data["mcp_servers"]["notifications"]
    assert entry["env_vars"] == ["MY_TELEGRAM_TOKEN"]
    assert entry["args"][-2:] == ["--token-env", "MY_TELEGRAM_TOKEN"]
    with pytest.raises(ValueError, match="requires stdio"):
        client_config(None, None, "codex", "http", token_env=DEFAULT_TELEGRAM_TOKEN_ENV)
    with pytest.raises(ValueError, match="supported for Codex"):
        client_config(None, None, "json", "stdio", token_env=DEFAULT_TELEGRAM_TOKEN_ENV)


def test_codex_cli_defaults_to_stdio():
    assert parser().parse_args(["connect", "codex"]).transport == "stdio"
    assert parser().parse_args(["setup", "--client", "codex"]).transport == "stdio"
    assert (
        parser().parse_args(["connect", "codex", "--token-env"]).token_env
        == DEFAULT_TELEGRAM_TOKEN_ENV
    )


def test_stdio_serve_builds_settings_from_forwarded_token(tmp_path, monkeypatch):
    import notification_mcp.server

    captured = {}

    async def serve(settings):
        captured["settings"] = settings

    monkeypatch.setattr(notification_mcp.server, "serve_stdio", serve)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv(DEFAULT_TELEGRAM_TOKEN_ENV, "123:private-token")
    monkeypatch.delenv("NOTIFICATION_MCP_CONFIG", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["notification-mcp", "serve", "--transport", "stdio"],
    )

    assert main() == 0
    assert captured["settings"].channels["personal"].token() == "123:private-token"
    assert not (tmp_path / "local/notification-mcp/config.toml").exists()


def test_explicit_config_wins_over_environment_profile(tmp_path, monkeypatch):
    import notification_mcp.server

    captured = {}

    async def serve(settings):
        captured["settings"] = settings

    config = tmp_path / "explicit.toml"
    setup_config(config, local=True)
    monkeypatch.setattr(notification_mcp.server, "serve_stdio", serve)
    monkeypatch.setenv(DEFAULT_TELEGRAM_TOKEN_ENV, "123:private-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "notification-mcp",
            "--config",
            str(config),
            "serve",
            "--transport",
            "stdio",
        ],
    )

    assert main() == 0
    assert captured["settings"].channels["personal"].type == "file"


def test_connect_cli_environment_profile_needs_no_service_config(tmp_path, monkeypatch, capsys):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("NOTIFICATION_MCP_CONFIG", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["notification-mcp", "connect", "codex", "--token-env"],
    )

    assert main() == 0

    entry = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))["mcp_servers"][
        "notifications"
    ]
    assert entry["env_vars"] == [DEFAULT_TELEGRAM_TOKEN_ENV]
    assert "--config" not in entry["args"]
    assert not (tmp_path / "local/notification-mcp/config.toml").exists()
    assert "no service config is used" in capsys.readouterr().out


def test_setup_can_configure_codex_stdio_in_one_command(tmp_path, monkeypatch, capsys):
    config = tmp_path / "personal/config.toml"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "notification-mcp",
            "--config",
            str(config),
            "setup",
            "--local",
            "--client",
            "codex",
        ],
    )

    assert main() == 0

    entry = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))["mcp_servers"][
        "notifications"
    ]
    assert entry["args"][-2:] == ["--transport", "stdio"]
    assert str(config) in entry["args"]
    output = capsys.readouterr().out
    assert "start and stop the server automatically" in output


@pytest.mark.parametrize(
    "text",
    [
        '[mcp_servers.notifications]\nurl="http://localhost:99"\n',
        '[mcp_servers.notifications]\nurl="http://127.0.0.1:8765/mcp"\nenabled=false\n',
        'mcp_servers = {docs = {url = "http://localhost:99"}}\n',
    ],
)
def test_codex_conflicts_leave_config_unchanged(settings, tmp_path, monkeypatch, text):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        connect_codex(settings, tmp_path / "service.toml")
    assert path.read_text(encoding="utf-8") == text


def test_client_examples_are_parseable_and_use_absolute_paths(settings, tmp_path):
    path = tmp_path / "config with spaces.toml"
    data = tomllib.loads(client_config(settings, path, "codex", "stdio"))
    entry = data["mcp_servers"]["notifications"]
    assert Path(entry["command"]).is_absolute()
    assert str(path) in entry["args"]
    assert entry["args"][-1] == "stdio"
    data = json.loads(client_config(settings, path, "json"))
    assert data["mcpServers"]["notifications"]["url"] == "http://127.0.0.1:8765/mcp"
