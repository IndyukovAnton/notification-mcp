import pytest
from pydantic import ValidationError

from notification_mcp.config import Settings, load_settings
from notification_mcp.models import Event, Notification


def test_config_paths_are_relative_to_config_file(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir()
    config = directory / "settings.toml"
    config.write_text(
        '[routing]\ndefault_channel="local"\n[channels.local]\ntype="file"\npath="output.jsonl"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    settings = load_settings(config)
    assert settings.storage.database == directory / "var/notifications.sqlite3"
    assert settings.channels["local"].path == directory / "output.jsonl"


@pytest.mark.parametrize(
    "change",
    [
        {"routing": {"default_channel": "missing"}},
        {"channels": {"local": {"type": "unknown"}}},
        {"server": {"host": "0.0.0.0"}},
        {"delivery": {"poll_interval_seconds": float("nan")}},
        {"delivery": {"retry_base_seconds": 100, "retry_max_seconds": 1}},
    ],
)
def test_invalid_configuration_is_rejected(settings, change):
    data = settings.model_dump(mode="json") | change
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_missing_telegram_secret_is_actionable_without_leaking_values(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_TELEGRAM_TOKEN", raising=False)
    config = tmp_path / "settings.toml"
    config.write_text(
        '[routing]\ndefault_channel="phone"\n[channels.phone]\ntype="telegram"\n'
        'chat_id="123456"\nbot_token_env="TEST_TELEGRAM_TOKEN"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="TEST_TELEGRAM_TOKEN"):
        load_settings(config)
    monkeypatch.setenv("TEST_TELEGRAM_TOKEN", "123:private-token")
    settings = load_settings(config)
    assert "private-token" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "   "},
        {"message": "x" * 3001},
        {"message": "hello", "url": "file:///private/file"},
        {"message": "hello", "url": "https://user:password@example.com"},
    ],
)
def test_notification_validation(payload):
    with pytest.raises(ValidationError):
        Notification(**payload)


def test_event_chooses_channel(settings):
    data = settings.model_dump(mode="json")
    data["channels"]["phone"] = {"type": "telegram", "chat_id": "123456"}
    data["routing"]["events"] = {"action_required": "phone"}
    configured = Settings.model_validate(data)
    assert configured.destination(Event.INFO)[0] == "local"
    assert configured.destination(Event.ACTION_REQUIRED)[0] == "phone"
