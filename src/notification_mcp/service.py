import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager

import httpx

from notification_mcp.channels import (
    DeliveryError,
    FileSender,
    Sender,
    TelegramAPI,
    TelegramSender,
    validate_notification,
)
from notification_mcp.config import DeliveryConfig, Settings, TelegramChannel, TelegramDestination
from notification_mcp.models import EnqueueResult, Notification
from notification_mcp.store import Store
from notification_mcp.telegram import TelegramSetup
from notification_mcp.worker_lock import WorkerLock

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        store: Store,
        senders: Mapping[str, Sender],
        config: DeliveryConfig,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.senders = senders
        self.config = config
        self.clock = clock
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()

    async def deliver_one(self) -> bool:
        delivery = self.store.claim(self.clock())
        if delivery is None:
            return False
        sender = self.senders[delivery.destination.type]
        error = None
        try:
            # Bound the whole attempt, including a slow-dripping HTTP response body.
            async with asyncio.timeout(self.config.request_timeout_seconds):
                await sender.send(delivery)
        except TimeoutError:
            error = DeliveryError("delivery_timeout", "Delivery timed out", retryable=True)
        except DeliveryError as exc:
            error = exc
        except Exception as exc:
            logger.error("Delivery adapter failed (%s)", type(exc).__name__)
            error = DeliveryError(
                "delivery_internal", "Unexpected delivery adapter error", retryable=True
            )
        now = self.clock()
        if error is None:
            self.store.sent(delivery, now, sender.min_interval_seconds)
            logger.info("Notification %s sent", delivery.id)
            return True
        retry_at = None
        if error.retryable and delivery.attempts < self.config.max_attempts:
            delay = min(
                self.config.retry_max_seconds,
                self.config.retry_base_seconds * 2 ** (delivery.attempts - 1),
            )
            # retry_after is a provider minimum, never capped by our backoff maximum.
            retry_at = now + max(delay, error.retry_after or 0)
        cooldown = now + max(sender.min_interval_seconds, error.retry_after or 0)
        self.store.failed_attempt(delivery, now, error.code, str(error), retry_at, cooldown)
        logger.warning("Notification %s: %s", delivery.id, error.code)
        return True

    async def run(self) -> None:
        while not self.stopping.is_set():
            self.wake.clear()
            if await self.deliver_one():
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), self.config.poll_interval_seconds)
            except TimeoutError:
                pass


class NotificationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.storage.database)
        self.worker: Worker | None = None
        self.worker_task: asyncio.Task | None = None
        self.setup_tasks: list[asyncio.Task] = []

    @property
    def healthy(self) -> bool:
        return (
            self.worker_task is not None
            and not self.worker_task.done()
            and all(not task.done() for task in self.setup_tasks)
        )

    def enqueue(
        self,
        notification: Notification,
        idempotency_key: str | None = None,
    ) -> EnqueueResult:
        if idempotency_key is not None:
            if not idempotency_key.strip() or len(idempotency_key) > 200:
                raise ValueError("Idempotency key must contain 1-200 nonblank characters")
        channel, destination = self.settings.destination(notification.event)
        if isinstance(destination, TelegramChannel):
            bot_id = destination.token().split(":", 1)[0]
            chat_id = destination.chat_id or self.store.telegram_chat(bot_id)
            if chat_id is None:
                raise ValueError(
                    f"Telegram channel '{channel}' is not connected: send /start to the bot first"
                )
            destination = TelegramDestination(
                bot_id=bot_id, chat_id=chat_id, bot_token_env=destination.bot_token_env
            )
        validate_notification(notification, destination)
        result = self.store.enqueue(
            notification, channel, destination, idempotency_key, time.time()
        )
        if self.worker:
            self.worker.wake.set()
        return result

    @asynccontextmanager
    async def running(self):
        with WorkerLock(self.settings.storage.database):
            self.store.initialize()
            self.store.recover_interrupted(time.time(), self.settings.delivery.max_attempts)
            tokens = {
                channel.token().split(":", 1)[0]: channel.token()
                for channel in self.settings.channels.values()
                if isinstance(channel, TelegramChannel)
            }
            async with httpx.AsyncClient(
                timeout=self.settings.delivery.request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                self.worker = Worker(
                    self.store,
                    {
                        "telegram": TelegramSender(
                            client, tokens, self.settings.delivery.request_timeout_seconds
                        ),
                        "file": FileSender(),
                    },
                    self.settings.delivery,
                )
                self.worker_task = asyncio.create_task(
                    self.worker.run(), name="notification-worker"
                )
                self.setup_tasks = [
                    asyncio.create_task(
                        TelegramSetup(
                            TelegramAPI(client), self.store, name, channel.token(), self.worker.wake
                        ).run(),
                        name=f"telegram-setup-{name}",
                    )
                    for name, channel in self.settings.channels.items()
                    if isinstance(channel, TelegramChannel) and channel.chat_id is None
                ]
                try:
                    yield self
                finally:
                    for task in self.setup_tasks:
                        task.cancel()
                    results = await asyncio.gather(*self.setup_tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error("Telegram setup stopped (%s)", type(result).__name__)
                    self.setup_tasks = []
                    self.worker.stopping.set()
                    self.worker.wake.set()
                    # Allow an in-flight bounded delivery to finish during graceful shutdown.
                    await self.worker_task
                    self.worker = None
                    self.worker_task = None
