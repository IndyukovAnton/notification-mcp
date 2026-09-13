import json
import os
import secrets
import socket
import subprocess
import sys

import httpx
import pytest

from notification_mcp.config import load_settings
from notification_mcp.installation import setup_config
from notification_mcp.runtime import Runtime, control_routes
from notification_mcp.server import create_http_app
from notification_mcp.worker_lock import WorkerLock


@pytest.fixture
def installed_config(tmp_path):
    config = tmp_path / "personal settings/config.toml"
    setup_config(config, local=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config.write_text(
        config.read_text().replace('"port" = 8765', f'"port" = {port}'), encoding="utf-8"
    )
    return config


def run_cli(config, *args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "notification_mcp", *args],
        env={**os.environ, "NOTIFICATION_MCP_CONFIG": str(config)},
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
    )


def test_background_lifecycle_from_another_directory(installed_config, tmp_path):
    runtime = Runtime(installed_config)
    settings = load_settings(installed_config)
    try:
        result = run_cli(installed_config, "start", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr + runtime.logs()
        assert json.loads(result.stdout)["status"] == "running"
        first_instance = runtime.read_state()["instance"]
        result = run_cli(installed_config, "start", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert runtime.read_state()["instance"] == first_instance
        status = run_cli(installed_config, "status")
        assert json.loads(status.stdout)["status"] == "running"
        # The installed test command verifies MCP discovery, notify, and status end to end.
        checked = run_cli(installed_config, "test")
        assert checked.returncode == 0, checked.stderr + runtime.logs()
        assert json.loads(checked.stdout)["status"] == "sent"
        result = run_cli(installed_config, "restart")
        assert result.returncode == 0, result.stderr + runtime.logs()
        assert runtime.read_state()["instance"] != first_instance
        result = run_cli(installed_config, "stop")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "stopped"
        assert run_cli(installed_config, "stop").returncode == 0
        assert runtime.status()["status"] == "stopped"
        with WorkerLock(settings.storage.database):
            pass
    finally:
        runtime.stop(wait_seconds=5)


def test_occupied_port_does_not_launch_or_control_another_process(installed_config):
    settings = load_settings(installed_config)
    with socket.socket() as occupied:
        occupied.bind((settings.server.host, settings.server.port))
        occupied.listen()
        with pytest.raises(ValueError, match="busy"):
            Runtime(installed_config).start(settings)
    assert not Runtime(installed_config).state_file.exists()


async def test_shutdown_endpoint_requires_instance_credentials(settings):
    stopped = []
    state = {"token": secrets.token_hex(32), "instance": secrets.token_hex(16)}

    async def shutdown():
        stopped.append(True)

    app = create_http_app(settings, extra_routes=control_routes(state, shutdown))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post("/_control/stop")).status_code == 401
            assert (
                await client.post("/_control/stop", headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            assert not stopped
            auth = {"Authorization": "Bearer " + state["token"]}
            response = await client.get("/_control", headers=auth)
            assert response.json() == {"instance": state["instance"]}
            assert state["token"] not in response.text
            assert (await client.post("/_control/stop", headers=auth)).status_code == 202
            assert stopped == [True]


def test_unreachable_worker_is_not_replaced_or_killed(installed_config):
    settings = load_settings(installed_config)
    runtime = Runtime(installed_config)
    runtime.directory.mkdir()
    state = {
        "url": f"http://127.0.0.1:{settings.server.port}",
        "token": secrets.token_hex(32),
        "instance": secrets.token_hex(16),
    }
    runtime.state_file.write_text(json.dumps(state))
    with WorkerLock(settings.storage.database):
        with pytest.raises(RuntimeError, match="already using"):
            runtime.stop()
        with pytest.raises(RuntimeError, match="already using"):
            runtime.start(settings)
    assert runtime.read_state() == state
    assert runtime.stop()["status"] == "stopped"


def test_missing_setup_is_actionable_without_traceback(tmp_path):
    result = run_cli(tmp_path / "missing.toml", "start")
    assert result.returncode == 2
    assert "notification-mcp setup" in result.stderr
    assert "Traceback" not in result.stderr
