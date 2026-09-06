"""Send one real notification through MCP, then check its delivery status."""

import argparse
import asyncio
import json
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def check(url: str, deadline_seconds: float) -> None:
    async with asyncio.timeout(deadline_seconds):
        # The service is local. Environment proxies must not intercept its MCP traffic.
        async with (
            httpx.AsyncClient(trust_env=False) as http_client,
            streamable_http_client(url, http_client=http_client) as (read, write, _),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {"notify", "notification_status"}
                result = await session.call_tool(
                    "notify",
                    {
                        "message": "Проверка MCP: доставка через выбранный в настройках канал.",
                        "event": "review_requested",
                        "source": "notification-mcp/check",
                        "idempotency_key": f"check:{uuid4().hex}",
                    },
                )
                if result.isError:
                    raise RuntimeError(str(result.content))
                receipt = result.structuredContent
                if not receipt:
                    raise RuntimeError("MCP did not return structured notification status")
                notification_id = receipt["notification"]["id"]
                while True:
                    status = await session.call_tool(
                        "notification_status", {"notification_id": notification_id}
                    )
                    if status.isError or not status.structuredContent:
                        raise RuntimeError("Could not read notification status")
                    state = status.structuredContent
                    if state["status"] in ("sent", "failed"):
                        print(json.dumps(state, ensure_ascii=False, indent=2))
                        if state["status"] == "failed":
                            raise RuntimeError("Notification delivery failed")
                        return
                    await asyncio.sleep(0.2)


if __name__ == "__main__":
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    arguments.add_argument("--timeout", type=float, default=30)
    options = arguments.parse_args()
    asyncio.run(check(options.url, options.timeout))
