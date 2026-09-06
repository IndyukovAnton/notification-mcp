"""Telegram-only setup commands; independent of the public notification contract."""

import asyncio
import logging
import time

from notification_mcp.channels import DeliveryError, TelegramAPI
from notification_mcp.config import TelegramDestination
from notification_mcp.models import Notification
from notification_mcp.store import Store

logger = logging.getLogger(__name__)
POLL_SECONDS = 25


class TelegramSetup:
    def __init__(
        self, api: TelegramAPI, store: Store, channel: str, token: str, wake: asyncio.Event
    ):
        self.api = api
        self.store = store
        self.channel = channel
        self.token = token
        self.bot_id = token.split(":", 1)[0]
        self.username: str | None = None
        self.wake = wake

    def handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text", "")
        if not isinstance(text, str):
            return
        parts = text.split(maxsplit=1)
        command = parts[0] if parts else ""
        if command.casefold() not in ("/start", f"/start@{self.username}".casefold()):
            return
        # Forwarded commands and anonymous group senders cannot claim/change ownership.
        if message.get("forward_origin") or message.get("sender_chat"):
            return
        sender, chat = message.get("from"), message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return
        user_id, chat_id = sender.get("id"), chat.get("id")
        if (
            type(user_id) is not int
            or user_id <= 0
            or sender.get("is_bot") is not False
            or type(chat_id) is not int
            or chat_id == 0
            or chat.get("type") not in ("private", "group", "supergroup")
        ):
            return
        accepted = self.store.bind_telegram(
            self.bot_id,
            str(user_id),
            str(chat_id),
            private=chat["type"] == "private",
            update_id=update["update_id"],
        )
        if not accepted:
            return
        self.store.enqueue(
            Notification(message="Чат подключён. Новые уведомления будут приходить сюда."),
            self.channel,
            TelegramDestination(bot_id=self.bot_id, chat_id=str(chat_id)),
            f"telegram-start:{self.bot_id}:{update['update_id']}",
            time.time(),
        )
        self.wake.set()

    async def poll_once(self) -> None:
        if self.username is None:
            bot = await self.api.call(self.token, "getMe", {})
            if (
                not isinstance(bot, dict)
                or str(bot.get("id")) != self.bot_id
                or not isinstance(bot.get("username"), str)
            ):
                raise DeliveryError("telegram_502", "Invalid Telegram bot identity", retryable=True)
            self.username = bot["username"]
            logger.info("Telegram channel %s: send /start to @%s", self.channel, self.username)
        payload = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
        offset = self.store.telegram_offset(self.bot_id)
        if offset is not None:
            payload["offset"] = offset
        updates = await self.api.call(
            self.token, "getUpdates", payload, deadline_seconds=POLL_SECONDS + 10
        )
        if not isinstance(updates, list) or any(
            not isinstance(update, dict) or type(update.get("update_id")) is not int
            for update in updates
        ):
            raise DeliveryError("telegram_502", "Invalid Telegram updates", retryable=True)
        for update in sorted(updates, key=lambda item: item["update_id"]):
            if offset is not None and update["update_id"] < offset:
                continue
            self.handle_update(update)
            # Acknowledge only after the binding and durable reply have been saved.
            offset = update["update_id"] + 1
            self.store.save_telegram_offset(self.bot_id, offset)

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await self.poll_once()
                failures = 0
                await asyncio.sleep(0.1)
            except DeliveryError as exc:
                failures += 1
                logger.warning("Telegram setup %s: %s (%s)", self.channel, exc.code, exc)
                delay = min(60, 2 ** min(failures, 6)) if exc.retryable else 60
                await asyncio.sleep(max(delay, exc.retry_after or 0))
