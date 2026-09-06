import asyncio
import json
import time

import httpx
import pytest

from notification_mcp.channels import DeliveryError, TelegramAPI, TelegramSender
from notification_mcp.config import (
    FileChannel,
    RoutingConfig,
    Settings,
    StorageConfig,
    TelegramChannel,
    TelegramDestination,
    load_settings,
)
from notification_mcp.models import Notification
from notification_mcp.service import NotificationService
from notification_mcp.store import Store
from notification_mcp.telegram import TelegramSetup

TOKEN = "123:local-secret"


@pytest.fixture
def telegram_service(tmp_path):
    settings = Settings(
        storage=StorageConfig(database=tmp_path / "queue.sqlite3"),
        routing=RoutingConfig(default_channel="phone", events={"info": "file"}),
        channels={
            "phone": TelegramChannel(bot_token=TOKEN),
            "file": FileChannel(path=tmp_path / "notes.jsonl"),
        },
    )
    service = NotificationService(settings)
    service.store.initialize()
    return service


def update(number, user=42, chat=42, kind="private", text="/start", **extra):
    return {
        "update_id": number,
        "message": {
            "from": {"id": user, "is_bot": False},
            "chat": {"id": chat, "type": kind},
            "text": text,
            **extra,
        },
    }


def setup(service, api=None):
    instance = TelegramSetup(api, service.store, "phone", TOKEN, asyncio.Event())
    instance.username = "notify_test_bot"
    return instance


def test_inline_token_survives_config_reload_without_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    config = tmp_path / "config.toml"
    config.write_text(
        '[routing]\ndefault_channel="phone"\n[channels.phone]\ntype="telegram"\n'
        f'bot_token="{TOKEN}"\n',
        encoding="utf-8",
    )
    for _ in range(2):
        settings = load_settings(config)
        assert settings.channels["phone"].token() == TOKEN
        assert settings.channels["phone"].chat_id is None
        assert TOKEN not in settings.model_dump_json()
        assert TOKEN not in repr(settings)


def test_start_binding_survives_restart_and_keeps_token_out_of_queue(telegram_service):
    service = telegram_service
    setup(service).handle_update(update(1))
    reopened = NotificationService(service.settings)
    note = reopened.enqueue(Notification(message="ready", event="review_requested"))
    assert reopened.store.telegram_chat("123") == "42"
    assert reopened.store.status(note.notification.id).status == "queued"
    with reopened.store.connection() as db:
        records = db.execute("SELECT destination FROM notifications").fetchall()
        assert len(records) == 2
        for row in records:
            assert TOKEN not in row[0]
            destination = json.loads(row[0])
            assert destination["chat_id"] == "42"
            assert destination["bot_id"] == "123"
            assert "bot_token" not in destination


def test_unconnected_telegram_does_not_block_other_channels(telegram_service):
    with pytest.raises(ValueError, match="send /start"):
        telegram_service.enqueue(Notification(message="ready", event="review_requested"))
    result = telegram_service.enqueue(Notification(message="local info"))
    assert result.notification.channel == "file"
    with telegram_service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_only_owner_can_change_chat_and_queued_recipients_stay_fixed(telegram_service):
    bot = setup(telegram_service)
    bot.handle_update(update(1, user=42, chat=-100, kind="supergroup"))
    assert telegram_service.store.telegram_chat("123") is None
    bot.handle_update(update(2))
    old = telegram_service.enqueue(Notification(message="old", event="review_requested"))
    bot.handle_update(update(3, user=99, chat=99))
    assert telegram_service.store.telegram_chat("123") == "42"
    bot.handle_update(update(4, chat=-100, kind="supergroup", text="/start@notify_test_bot"))
    assert telegram_service.store.telegram_chat("123") == "-100"
    new = telegram_service.enqueue(Notification(message="new", event="review_requested"))
    with telegram_service.store.connection() as db:
        rows = db.execute("SELECT id,destination FROM notifications").fetchall()
        targets = {row["id"]: json.loads(row["destination"])["chat_id"] for row in rows}
    assert targets[old.notification.id] == "42"
    assert targets[new.notification.id] == "-100"
    bot.handle_update(update(5))
    assert telegram_service.store.telegram_chat("123") == "42"


@pytest.mark.parametrize(
    "message",
    [
        update(1, text="hello"),
        update(1, text="   "),
        update(1, text="/start@other_bot"),
        update(1, text="/starter"),
        update(1, forward_origin={"type": "user"}),
        update(1, sender_chat={"id": -100}),
        update(1, kind="channel"),
        update(1, user=0),
        update(1, chat=100),
        {"update_id": 1, "edited_message": update(1)["message"]},
    ],
)
def test_unrelated_or_unauthenticated_messages_do_not_bind(telegram_service, message):
    setup(telegram_service).handle_update(message)
    assert telegram_service.store.telegram_chat("123") is None


async def test_polling_persists_offset_and_replies_once_after_replay(telegram_service):
    calls = []

    def handler(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        if request.url.path.endswith("getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 123,
                        "username": "notify_test_bot",
                    },
                },
            )
        return httpx.Response(200, json={"ok": True, "result": [update(7)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramSetup(
            TelegramAPI(client), telegram_service.store, "phone", TOKEN, asyncio.Event()
        )
        await bot.poll_once()
        reopened = Store(telegram_service.store.path)
        assert reopened.telegram_offset("123") == 8
        second = TelegramSetup(TelegramAPI(client), reopened, "phone", TOKEN, asyncio.Event())
        await second.poll_once()
    assert calls[-1][1]["offset"] == 8
    assert calls[-1][1]["allowed_updates"] == ["message"]
    with reopened.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


async def test_api_failure_does_not_acknowledge_updates_or_leak_secret(telegram_service):
    def handler(request):
        raise httpx.ConnectError(f"Failure {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = setup(telegram_service, TelegramAPI(client))
        with pytest.raises(DeliveryError) as captured:
            await bot.poll_once()
    assert TOKEN not in str(captured.value)
    assert telegram_service.store.telegram_offset("123") is None


async def test_inline_credentials_send_after_reopening_database(telegram_service, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    setup(telegram_service).handle_update(update(1))
    delivery = Store(telegram_service.store.path).claim(time.time())

    def handler(request):
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        assert json.loads(request.content)["chat_id"] == "42"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await TelegramSender(client, {"123": TOKEN}).send(delivery)


async def test_different_bot_cannot_send_old_notifications(telegram_service, monkeypatch):
    setup(telegram_service).handle_update(update(1))
    delivery = telegram_service.store.claim(time.time())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:other-bot")

    def unexpected(request):
        pytest.fail("Must not send with credentials for another bot")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(DeliveryError, match="token is not configured"):
            await TelegramSender(client, {"999": "999:other-bot"}).send(delivery)


async def test_service_receives_start_delivers_and_cancels_long_poll(telegram_service, monkeypatch):
    sent = asyncio.Event()
    polling = asyncio.Event()
    sent_messages = []
    polls = 0

    async def handler(request):
        nonlocal polls
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            result = {"id": 123, "username": "notify_test_bot"}
        elif method == "getUpdates":
            polls += 1
            if polls > 1:
                polling.set()
                await asyncio.Event().wait()
            result = [update(1)]
        else:
            sent_messages.append(json.loads(request.content))
            sent.set()
            result = {"message_id": 1}
        return httpx.Response(200, json={"ok": True, "result": result})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("notification_mcp.service.httpx.AsyncClient", lambda **_: client)
    async with asyncio.timeout(3):
        async with telegram_service.running():
            await sent.wait()
            await polling.wait()
            assert telegram_service.healthy
            assert telegram_service.store.telegram_chat("123") == "42"
            assert sent_messages[0]["chat_id"] == "42"
        assert not telegram_service.healthy
        assert telegram_service.setup_tasks == []


def test_upgrade_existing_queue_preserves_notifications(service):
    note = service.enqueue(Notification(message="existing"))
    with service.store.connection() as db:
        db.execute("DROP TABLE telegram_bindings")
        db.execute("DROP TABLE telegram_offsets")
        db.execute("PRAGMA user_version=1")
    reopened = Store(service.store.path)
    reopened.initialize()
    assert reopened.status(note.notification.id).status == "queued"
    assert reopened.telegram_chat("123") is None
    assert reopened.telegram_offset("123") is None


def test_legacy_destination_still_loads_without_inline_secret():
    from notification_mcp.store import CHANNEL_ADAPTER

    destination = CHANNEL_ADAPTER.validate_json(
        '{"type":"telegram","chat_id":"42","bot_token_env":"OLD_TOKEN"}'
    )
    assert isinstance(destination, TelegramDestination)
    assert destination.bot_id is None


def test_duplicate_start_after_interruption_does_not_duplicate_confirmation(telegram_service):
    setup(telegram_service).handle_update(update(1))
    # Simulate a crash after enqueueing the confirmation, before saving the polling offset.
    assert telegram_service.store.telegram_offset("123") is None
    setup(telegram_service).handle_update(update(1))
    with telegram_service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


@pytest.mark.parametrize("status,retry_after,expected_delay", [(429, 90, 90), (409, None, 60)])
async def test_polling_reconnects_and_respects_provider_delay(
    telegram_service, monkeypatch, status, retry_after, expected_delay, caplog
):
    from notification_mcp import telegram

    delays = []
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                status,
                json={
                    "ok": False,
                    "description": TOKEN,
                    "parameters": {"retry_after": retry_after},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": [update(1)]})

    async def skip_delay(seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(telegram.asyncio, "sleep", skip_delay)
        with pytest.raises(asyncio.CancelledError):
            await setup(telegram_service, TelegramAPI(client)).run()
    assert delays[0] == expected_delay
    assert telegram_service.store.telegram_chat("123") == "42"
    assert TOKEN not in caplog.text


def test_one_bot_cannot_have_two_automatic_channels(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[routing]\ndefault_channel="one"\n'
        f'[channels.one]\ntype="telegram"\nbot_token="{TOKEN}"\n'
        f'[channels.two]\ntype="telegram"\nbot_token="{TOKEN}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="one automatic"):
        load_settings(config)


async def test_fixed_recipient_does_not_start_polling(telegram_service, monkeypatch):
    telegram_service.settings.channels["phone"] = TelegramChannel(bot_token=TOKEN, chat_id="777")

    def unexpected(request):
        pytest.fail("No Telegram requests until a notification is queued")

    client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
    monkeypatch.setattr("notification_mcp.service.httpx.AsyncClient", lambda **_: client)
    async with telegram_service.running():
        assert telegram_service.setup_tasks == []
