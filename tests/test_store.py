import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from notification_mcp.config import FileChannel
from notification_mcp.models import Notification
from notification_mcp.store import Store
from notification_mcp.worker_lock import WorkerLock


def test_queue_survives_reopening_and_freezes_destination(service, tmp_path):
    result = service.enqueue(Notification(message="ready"), "task:done")
    # Later configuration changes do not redirect accepted notifications.
    service.settings.channels["local"] = FileChannel(path=tmp_path / "different.jsonl")
    reopened = Store(service.store.path)
    delivery = reopened.claim(time.time())
    assert delivery.id == result.notification.id
    assert delivery.destination.path == tmp_path / "notifications.jsonl"
    assert reopened.status(delivery.id).status == "sending"


def test_concurrent_idempotency_creates_one_record(service):
    notification = Notification(message="ready", source="project")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.enqueue(notification, "same-key"), range(8)))
    assert len({result.notification.id for result in results}) == 1
    assert sum(not result.deduplicated for result in results) == 1
    with pytest.raises(ValueError, match="different notification"):
        service.enqueue(Notification(message="different"), "same-key")


def test_repeated_messages_without_key_are_distinct(service):
    note = Notification(message="ready")
    assert service.enqueue(note).notification.id != service.enqueue(note).notification.id


@pytest.mark.parametrize("max_attempts,expected", [(2, "queued"), (1, "failed")])
def test_interrupted_attempt_recovers_without_stuck_sending(service, max_attempts, expected):
    result = service.enqueue(Notification(message="ready"))
    now = time.time()
    delivery = service.store.claim(now)
    assert delivery.attempts == 1
    reopened = Store(service.store.path)
    reopened.recover_interrupted(now + 1, max_attempts)
    status = reopened.status(result.notification.id)
    assert status.status == expected
    assert status.last_error_code == "delivery_interrupted"


def test_only_one_worker_can_own_database(tmp_path):
    database = tmp_path / "queue.sqlite3"
    with WorkerLock(database):
        with pytest.raises(RuntimeError, match="already using"):
            with WorkerLock(database):
                pytest.fail("second worker must not acquire the lock")
    with WorkerLock(database):
        pass


def test_unknown_notification_is_reported(service):
    with pytest.raises(ValueError, match="not found"):
        service.store.status("missing")
