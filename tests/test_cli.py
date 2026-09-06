import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def write_config(tmp_path, port=8765):
    config = tmp_path / "config.toml"
    config.write_text(
        f"[server]\nport={port}\n[delivery]\npoll_interval_seconds=0.1\n"
        '[routing]\ndefault_channel="local"\n'
        '[channels.local]\ntype="file"\npath="output.jsonl"\n',
        encoding="utf-8",
    )
    return config


def command(config, *args):
    return [sys.executable, "-m", "notification_mcp", "--config", str(config), *args]


async def test_stdio_process_supports_notifications(tmp_path):
    config = write_config(tmp_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=command(config, "serve", "--transport", "stdio")[1:],
    )
    async with asyncio.timeout(15):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("notify", {"message": "stdio works"})
                assert not result.isError
                notification_id = result.structuredContent["notification"]["id"]
                while True:
                    result = await session.call_tool(
                        "notification_status", {"notification_id": notification_id}
                    )
                    if result.structuredContent["status"] == "sent":
                        break
                    await asyncio.sleep(0.02)
    assert json.loads((tmp_path / "output.jsonl").read_text())["message"] == "stdio works"


def test_http_process_runs_documented_check_script_and_restores_queue(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = write_config(tmp_path, port)
    # Enqueue through the CLI while the worker is stopped, then start a separate server.
    queued = subprocess.run(
        command(config, "notify", "--message", "queued before startup"),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    receipt = json.loads(queued.stdout)
    assert receipt["notification"]["status"] == "queued"
    output = tmp_path / "server.log"
    with output.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command(config, "serve"),
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 10
            with httpx.Client(timeout=0.5, trust_env=False) as client:
                while True:
                    assert process.poll() is None, output.read_text(encoding="utf-8")
                    try:
                        if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "Server startup timed out"
                    time.sleep(0.05)
            script = Path(__file__).resolve().parents[1] / "scripts/check_mcp.py"
            checked = subprocess.run(
                [sys.executable, str(script), "--url", f"http://127.0.0.1:{port}/mcp"],
                capture_output=True,
                text=True,
                check=False,
                timeout=35,
                env={
                    **os.environ,
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "ALL_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                },
            )
            assert checked.returncode == 0, checked.stderr
            assert json.loads(checked.stdout)["status"] == "sent"
            status = subprocess.run(
                command(config, "status", receipt["notification"]["id"]),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            assert json.loads(status.stdout)["status"] == "sent"
        finally:
            process.terminate()
            process.wait(timeout=5)
    records = [
        json.loads(line)
        for line in (tmp_path / "output.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert records[0]["message"] == "queued before startup"


def test_config_error_never_prints_inline_secret(tmp_path):
    config = write_config(tmp_path)
    with config.open("a", encoding="utf-8") as stream:
        stream.write('bot_token="secret-do-not-print"\n')
    result = subprocess.run(
        command(config, "check-config"), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert "secret-do-not-print" not in result.stderr
    assert "extra_forbidden" in result.stderr
