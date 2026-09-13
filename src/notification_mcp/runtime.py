"""Start and gracefully stop a local server without relying on reusable process IDs."""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
from starlette.routing import Route

from notification_mcp.config import Settings
from notification_mcp.installation import atomic_write
from notification_mcp.integrations import server_url
from notification_mcp.worker_lock import WorkerLock

STARTUP_SECONDS = 15


class Runtime:
    def __init__(self, config: Path):
        self.config = config.expanduser().resolve()
        self.directory = self.config.with_suffix(".runtime")
        self.state_file = self.directory / "server.json"
        self.log_file = self.directory / "server.log"

    def read_state(self) -> dict | None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            raise ValueError(f"Cannot read server state: {self.state_file}") from None
        if (
            not isinstance(data, dict)
            or not re.fullmatch(
                r"http://(127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}", str(data.get("url", ""))
            )
            or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("token", "")))
            or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("instance", "")))
        ):
            raise ValueError(f"Invalid server state: {self.state_file}")
        return data

    @staticmethod
    def probe(state: dict) -> bool:
        try:
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                response = client.get(
                    state["url"] + "/_control",
                    headers={
                        "Authorization": "Bearer " + state["token"],
                    },
                )
            return (
                response.status_code == 200 and response.json().get("instance") == state["instance"]
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def status(self) -> dict:
        state = self.read_state()
        return {
            "status": "running"
            if state and self.probe(state)
            else "unreachable"
            if state
            else "stopped",
            "url": state["url"] + "/mcp" if state else None,
            "config": str(self.config),
            "log": str(self.log_file),
        }

    def clear(self, instance: str) -> None:
        state = self.read_state()
        if state is not None and state["instance"] == instance:
            self.state_file.unlink(missing_ok=True)

    def start(self, settings: Settings) -> dict:
        with WorkerLock(self.directory / "lifecycle"):
            return self._start(settings)

    def _start(self, settings: Settings) -> dict:
        previous = self.read_state()
        if previous and self.probe(previous):
            return self.status()
        # A foreground server or a temporarily unresponsive worker must not be replaced.
        with WorkerLock(settings.storage.database):
            pass
        family = socket.AF_INET6 if settings.server.host == "::1" else socket.AF_INET
        try:
            with socket.socket(family) as probe:
                probe.bind((settings.server.host, settings.server.port))
        except OSError:
            raise ValueError(
                f"Port {settings.server.port} is busy. "
                "Stop the previous server or change server.port"
            ) from None
        state = {
            "url": server_url(settings),
            "token": secrets.token_hex(32),
            "instance": secrets.token_hex(16),
        }
        atomic_write(self.state_file, json.dumps(state))
        command = [
            sys.executable,
            "-m",
            "notification_mcp",
            "--config",
            str(self.config),
            "serve",
            "--managed",
        ]
        try:
            with self.log_file.open("ab") as log:
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=self.config.parent,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
        except OSError:
            self.clear(state["instance"])
            raise
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if child.poll() is not None:
                self.clear(state["instance"])
                raise RuntimeError(f"Server could not start. Read the log: {self.log_file}")
            if self.probe(state):
                return self.status()
            time.sleep(0.1)
        # Keep the state so a late-starting server remains controllable.
        raise RuntimeError(
            "Startup is taking longer than expected. Run status or logs before retrying"
        )

    def stop(self, wait_seconds: float = 75) -> dict:
        with WorkerLock(self.directory / "lifecycle"):
            return self._stop(wait_seconds)

    def _stop(self, wait_seconds: float) -> dict:
        state = self.read_state()
        if state is None:
            return self.status()
        if not self.probe(state):
            # Never kill a PID or delete state while the worker might still be alive.
            from notification_mcp.config import load_settings

            settings = load_settings(self.config, check_secrets=False)
            with WorkerLock(settings.storage.database):
                self.clear(state["instance"])
            return self.status()
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.post(
                    state["url"] + "/_control/stop",
                    headers={
                        "Authorization": "Bearer " + state["token"],
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            raise RuntimeError(
                "Could not confirm the stop request. Run status and retry stop"
            ) from None
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            current = self.read_state()
            if current is None or current["instance"] != state["instance"]:
                return self.status()
            time.sleep(0.1)
        raise RuntimeError("Server is still stopping. Run status or logs; no process was killed")

    def restart(self, settings: Settings) -> dict:
        with WorkerLock(self.directory / "lifecycle"):
            self._stop(settings.delivery.request_timeout_seconds + 10)
            return self._start(settings)

    def logs(self, lines: int = 50) -> str:
        try:
            with self.log_file.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 65536))
                text = stream.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return "No log yet. Run notification-mcp start."
        return "\n".join(text.splitlines()[-lines:])


def control_routes(state: dict, shutdown) -> list[Route]:
    def authorized(request) -> bool:
        value = request.headers.get("Authorization", "")
        return secrets.compare_digest(value, "Bearer " + state["token"])

    async def status(request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"instance": state["instance"]})

    async def stop(request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(
            {"status": "stopping"}, status_code=202, background=BackgroundTask(shutdown)
        )

    return [Route("/_control", status), Route("/_control/stop", stop, methods=["POST"])]


def run_managed(config: Path, settings: Settings) -> None:
    import uvicorn

    from notification_mcp.server import create_http_app

    runtime = Runtime(config)
    state = runtime.read_state()
    if state is None or state["url"] != server_url(settings):
        raise ValueError("Managed servers must be launched with notification-mcp start")

    async def shutdown():
        server.should_exit = True

    app = create_http_app(settings, extra_routes=control_routes(state, shutdown))
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.server.host,
            port=settings.server.port,
            log_config=None,
            access_log=False,
        )
    )
    try:
        server.run()
    finally:
        runtime.clear(state["instance"])
