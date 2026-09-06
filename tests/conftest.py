import pytest

from notification_mcp.config import FileChannel, RoutingConfig, Settings, StorageConfig
from notification_mcp.service import NotificationService


@pytest.fixture
def settings(tmp_path):
    return Settings(
        storage=StorageConfig(database=tmp_path / "queue.sqlite3"),
        routing=RoutingConfig(default_channel="local"),
        channels={"local": FileChannel(path=tmp_path / "notifications.jsonl")},
    )


@pytest.fixture
def service(settings):
    service = NotificationService(settings)
    service.store.initialize()
    return service
