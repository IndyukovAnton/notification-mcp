import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from notification_mcp.models import Notification
from notification_mcp.server import create_http_app


async def test_mcp_handshake_notification_status_and_deduplication(settings, tmp_path):
    app = create_http_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert {tool.name for tool in tools} == {"notify", "notification_status"}
                    notify_schema = next(t for t in tools if t.name == "notify").inputSchema
                    assert "channel" not in notify_schema["properties"]
                    assert "chat_id" not in notify_schema["properties"]
                    payload = {
                        "message": "Ready to inspect",
                        "event": "review_requested",
                        "source": "tests",
                        "idempotency_key": "project:task:done",
                    }
                    result = await session.call_tool("notify", payload)
                    assert not result.isError
                    assert result.structuredContent["notification"]["status"] == "queued"
                    notification_id = result.structuredContent["notification"]["id"]
                    repeated = await session.call_tool("notify", payload)
                    assert repeated.structuredContent["deduplicated"] is True
                    assert repeated.structuredContent["notification"]["id"] == notification_id
                    async with asyncio.timeout(3):
                        while True:
                            status = await session.call_tool(
                                "notification_status", {"notification_id": notification_id}
                            )
                            assert not status.isError
                            if status.structuredContent["status"] == "sent":
                                break
                            await asyncio.sleep(0.02)
                    invalid = await session.call_tool("notify", {"message": " "})
                    assert invalid.isError
                    missing = await session.call_tool(
                        "notification_status", {"notification_id": "unknown"}
                    )
                    assert missing.isError
    records = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["source"] == "tests"


async def test_http_rejects_untrusted_origin_and_host(settings):
    app = create_http_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            headers = {"Content-Type": "application/json", "Origin": "https://untrusted.example"}
            assert (await client.post("/mcp", json={}, headers=headers)).status_code == 403
            assert (
                await client.post("/mcp", json={}, headers={"Host": "untrusted.example"})
            ).status_code == 421


async def test_server_recovers_previously_interrupted_delivery(service, settings, tmp_path):
    import time

    result = service.enqueue(Notification(message="Survives restart"))
    service.store.claim(time.time())
    app = create_http_app(settings)
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(3):
            # Observe persisted state through a separate Store, as another process would.
            while service.store.status(result.notification.id).status != "sent":  # noqa: ASYNC110
                await asyncio.sleep(0.02)
    assert service.store.status(result.notification.id).attempts == 2
    assert (
        json.loads((tmp_path / "notifications.jsonl").read_text())["message"] == "Survives restart"
    )
