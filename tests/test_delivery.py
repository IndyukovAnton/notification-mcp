import asyncio
import json
import time

import pytest

from notification_mcp.channels import DeliveryError, FileSender
from notification_mcp.config import DeliveryConfig, FileChannel
from notification_mcp.models import Notification
from notification_mcp.service import Worker


class FakeSender:
    min_interval_seconds = 0

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.ids = []

    async def send(self, delivery):
        self.ids.append(delivery.id)
        if self.failures:
            raise self.failures.pop(0)


def make_worker(service, sender, now, **config):
    return Worker(service.store, {"file": sender}, DeliveryConfig(**config), clock=lambda: now[0])


async def test_transient_failure_retries_then_succeeds(service):
    result = service.enqueue(Notification(message="ready"))
    now = [time.time()]
    sender = FakeSender([DeliveryError("offline", "Offline", retryable=True)])
    worker = make_worker(service, sender, now, retry_base_seconds=2)
    assert await worker.deliver_one()
    assert service.store.status(result.notification.id).status == "queued"
    assert not await worker.deliver_one()
    now[0] += 2
    assert await worker.deliver_one()
    status = service.store.status(result.notification.id)
    assert status.status == "sent"
    assert status.attempts == 2
    assert status.last_error is None


@pytest.mark.parametrize("retryable,max_attempts", [(False, 8), (True, 1)])
async def test_permanent_error_and_exhausted_attempts_stop(service, retryable, max_attempts):
    result = service.enqueue(Notification(message="ready"))
    sender = FakeSender([DeliveryError("denied", "Denied", retryable=retryable)])
    now = [time.time()]
    worker = make_worker(service, sender, now, max_attempts=max_attempts)
    await worker.deliver_one()
    status = service.store.status(result.notification.id)
    assert status.status == "failed"
    assert status.next_attempt_at is None
    now[0] += 1000
    assert not await worker.deliver_one()


async def test_rate_limit_pauses_destination_but_not_other_channels(service, tmp_path):
    first = service.enqueue(Notification(message="one"))
    second = service.enqueue(Notification(message="two"))
    other = service.store.enqueue(
        Notification(message="other channel"),
        "other",
        FileChannel(path=tmp_path / "other"),
        None,
        time.time(),
    )
    now = [time.time()]
    sender = FakeSender([DeliveryError("rate_limit", "Slow down", retryable=True, retry_after=60)])
    worker = make_worker(service, sender, now, retry_base_seconds=1, retry_max_seconds=2)
    await worker.deliver_one()
    await worker.deliver_one()
    assert service.store.status(first.notification.id).status == "queued"
    assert service.store.status(second.notification.id).attempts == 0
    assert service.store.status(other.notification.id).status == "sent"
    now[0] += 59
    assert not await worker.deliver_one()
    now[0] += 1
    assert await worker.deliver_one()


async def test_file_channel_delivers_payload_and_event(service, tmp_path):
    result = service.enqueue(Notification(message="Готово", event="review_requested"))
    worker = make_worker(service, FileSender(), [time.time()])
    assert await worker.deliver_one()
    record = json.loads((tmp_path / "notifications.jsonl").read_text(encoding="utf-8"))
    assert record["id"] == result.notification.id
    assert record["message"] == "Готово"
    assert record["event"] == "review_requested"


async def test_whole_attempt_has_deadline(service):
    class SlowSender(FakeSender):
        async def send(self, delivery):
            await asyncio.Event().wait()

    result = service.enqueue(Notification(message="ready"))
    worker = make_worker(service, SlowSender(), [time.time()], request_timeout_seconds=0.1)
    await worker.deliver_one()
    assert service.store.status(result.notification.id).last_error_code == "delivery_timeout"


async def test_unexpected_error_does_not_leak_secrets(service):
    result = service.enqueue(Notification(message="ready"))
    worker = make_worker(service, FakeSender([RuntimeError("secret-token")]), [time.time()])
    await worker.deliver_one()
    status = service.store.status(result.notification.id)
    assert status.last_error_code == "delivery_internal"
    assert "secret-token" not in status.model_dump_json()
