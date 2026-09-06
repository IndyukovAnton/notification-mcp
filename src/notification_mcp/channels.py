import asyncio
import json
import os
from collections.abc import Mapping
from typing import Protocol

import httpx

from notification_mcp.config import Destination, FileChannel, TelegramDestination
from notification_mcp.models import Event, Notification
from notification_mcp.store import Delivery


class DeliveryError(Exception):
    def __init__(
        self, code: str, message: str, *, retryable: bool, retry_after: float | None = None
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class Sender(Protocol):
    min_interval_seconds: float

    async def send(self, delivery: Delivery) -> None: ...


EVENT_LABELS = {
    Event.INFO: "Информация",
    Event.ACTION_REQUIRED: "Требуется решение",
    Event.REVIEW_REQUESTED: "Готово к проверке",
    Event.ERROR: "Ошибка",
}


def render_text(notification: Notification) -> str:
    lines = [EVENT_LABELS[notification.event]]
    if notification.title:
        lines.append(notification.title)
    if notification.source:
        lines.append(f"Источник: {notification.source}")
    lines.extend(["", notification.message])
    if notification.url:
        lines.extend(["", str(notification.url)])
    return "\n".join(lines)


class TelegramAPI:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def call(self, token: str, method: str, payload: dict, *, deadline_seconds: float = 10):
        try:
            async with asyncio.timeout(deadline_seconds):
                response = await self.client.post(
                    f"https://api.telegram.org/bot{token}/{method}",
                    json=payload,
                    timeout=deadline_seconds,
                )
        except (httpx.TransportError, TimeoutError):
            # Exception strings can contain the token-bearing URL. Never persist or log them.
            raise DeliveryError(
                "telegram_connection", "Telegram connection failed or timed out", retryable=True
            ) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        code = response.status_code
        if response.is_success and data.get("ok") is True:
            if "result" in data:
                return data["result"]
        if response.is_success:
            code = data.get("error_code", 502)
        if not isinstance(code, int):
            code = 502
        retry_after = None
        parameters = data.get("parameters")
        if code == 429 and isinstance(parameters, dict):
            value = parameters.get("retry_after")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                retry_after = float(value)
        retryable = code in (408, 425, 429) or code >= 500
        descriptions = {
            400: "Telegram rejected the message or recipient",
            401: "Telegram rejected the bot token",
            403: "Telegram bot cannot send to this recipient; check /start and bot access",
            409: "Telegram polling conflicts with a webhook or another getUpdates consumer",
            429: "Telegram rate limit reached",
        }
        raise DeliveryError(
            f"telegram_{code}",
            descriptions.get(code, "Telegram did not confirm message delivery"),
            retryable=retryable,
            retry_after=retry_after,
        )


class TelegramSender:
    min_interval_seconds = 1.1

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: Mapping[str, str] | None = None,
        request_timeout_seconds: float = 10,
    ):
        self.api = TelegramAPI(client)
        self.tokens = tokens or {}
        self.request_timeout_seconds = request_timeout_seconds

    @staticmethod
    def validate(notification: Notification, destination: Destination) -> None:
        if not isinstance(destination, TelegramDestination):
            raise ValueError("Invalid Telegram destination")
        try:
            length = len(render_text(notification).encode("utf-16-le")) // 2
        except UnicodeEncodeError:
            raise ValueError("Notification contains invalid Unicode") from None
        if length > 4096:
            raise ValueError("Formatted Telegram notification exceeds 4096 UTF-16 units")

    async def send(self, delivery: Delivery) -> None:
        destination = delivery.destination
        self.validate(delivery.notification, destination)
        token = self.tokens.get(destination.bot_id, "")
        if not token:
            token = os.environ.get(destination.bot_token_env, "").strip()
        if not token or (destination.bot_id and token.split(":", 1)[0] != destination.bot_id):
            raise DeliveryError(
                "missing_credentials", "Telegram bot token is not configured", retryable=False
            )
        result = await self.api.call(
            token,
            "sendMessage",
            {"chat_id": destination.chat_id, "text": render_text(delivery.notification)},
            deadline_seconds=self.request_timeout_seconds,
        )
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise DeliveryError(
                "telegram_502", "Telegram did not confirm message delivery", retryable=True
            )


class FileSender:
    """Offline diagnostic destination, deliberately separate from Telegram."""

    min_interval_seconds = 0.0

    @staticmethod
    def validate(notification: Notification, destination: Destination) -> None:
        if not isinstance(destination, FileChannel):
            raise ValueError("Invalid file channel configuration")
        # Reject invalid Unicode before accepting an undeliverable item into the queue.
        notification.model_dump_json().encode("utf-8")

    async def send(self, delivery: Delivery) -> None:
        self.validate(delivery.notification, delivery.destination)

        def append() -> None:
            path = delivery.destination.path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {"id": delivery.id, **delivery.notification.model_dump(mode="json")},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())

        try:
            await asyncio.to_thread(append)
        except OSError:
            raise DeliveryError(
                "file_write", "Cannot write to the configured file destination", retryable=True
            ) from None


# Add an adapter here and its configuration model to config.Channel to extend delivery.
ADAPTER_TYPES = {"telegram": TelegramSender, "file": FileSender}


def validate_notification(notification: Notification, destination: Destination) -> None:
    ADAPTER_TYPES[destination.type].validate(notification, destination)
