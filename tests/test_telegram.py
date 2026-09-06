import json

import httpx
import pytest

from notification_mcp.channels import DeliveryError, TelegramSender
from notification_mcp.config import TelegramDestination
from notification_mcp.models import Notification
from notification_mcp.store import Delivery


def delivery(message="Готово: тесты (12/12)! *Без Markdown*"):
    return Delivery(
        id="test",
        notification=Notification(message=message, event="review_requested"),
        destination=TelegramDestination(chat_id="123456", bot_token_env="TEST_BOT_TOKEN"),
        channel_key="test",
        attempts=1,
    )


async def test_telegram_uses_configured_recipient_and_plain_text(monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN", "123:private-token")

    def handler(request):
        assert request.url.host == "api.telegram.org"
        assert request.url.path == "/bot123:private-token/sendMessage"
        body = json.loads(request.content)
        assert body["chat_id"] == "123456"
        assert "*Без Markdown*" in body["text"]
        assert "parse_mode" not in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await TelegramSender(client).send(delivery())


@pytest.mark.parametrize(
    "status,payload,retryable,retry_after",
    [
        (429, {"ok": False, "parameters": {"retry_after": 45}}, True, 45),
        (503, {"ok": False}, True, None),
        (403, {"ok": False, "description": "private-token"}, False, None),
        (401, {"ok": False}, False, None),
        (400, {"ok": False}, False, None),
        (200, {"ok": False, "error_code": 429, "parameters": {"retry_after": 90}}, True, 90),
        (200, {"ok": True}, True, None),
        (200, [], True, None),
    ],
)
async def test_telegram_failure_classification(
    monkeypatch, status, payload, retryable, retry_after
):
    monkeypatch.setenv("TEST_BOT_TOKEN", "123:private-token")
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DeliveryError) as captured:
            await TelegramSender(client).send(delivery())
    assert captured.value.retryable is retryable
    assert captured.value.retry_after == retry_after
    assert "private-token" not in str(captured.value)


async def test_network_exception_hides_bot_token(monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN", "123:private-token")

    def offline(request):
        raise httpx.ConnectError(f"Cannot reach {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
        with pytest.raises(DeliveryError, match="connection failed") as captured:
            await TelegramSender(client).send(delivery())
    assert captured.value.retryable
    assert "private-token" not in str(captured.value)


def test_telegram_rejects_oversized_unicode_before_sending():
    note = delivery(message="😀" * 2200)
    with pytest.raises(ValueError, match="4096"):
        TelegramSender.validate(note.notification, note.destination)


async def test_missing_token_is_permanent_and_does_not_make_request(monkeypatch):
    monkeypatch.delenv("TEST_BOT_TOKEN", raising=False)

    def unexpected(request):
        pytest.fail("No network request expected without credentials")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(DeliveryError) as captured:
            await TelegramSender(client).send(delivery())
    assert not captured.value.retryable
