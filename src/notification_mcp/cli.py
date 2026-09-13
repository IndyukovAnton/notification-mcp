import argparse
import asyncio
import getpass
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from notification_mcp import __version__
from notification_mcp.config import DEFAULT_TELEGRAM_TOKEN_ENV, load_settings
from notification_mcp.installation import environment_settings, resolve_config, setup_config
from notification_mcp.models import Event, Notification
from notification_mcp.service import NotificationService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local, channel-independent notification service")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument(
        "--config", type=Path, help="Use an explicit config instead of personal settings"
    )
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "setup", help="One-time setup: enter a token and save personal settings"
    )
    setup_mode = setup.add_mutually_exclusive_group()
    setup_mode.add_argument("--local", action="store_true", help="Use a file for an offline check")
    setup_mode.add_argument(
        "--import-config", type=Path, help="Import settings and queue; stop the old server first"
    )
    setup.add_argument("--client", choices=["codex"], help="Also register the server in a client")
    setup.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="stdio",
        help="Transport for --client (default: stdio)",
    )
    commands.add_parser("start", help="Start the server in the background")
    commands.add_parser("stop", help="Gracefully stop the background server")
    commands.add_parser("restart", help="Restart the background server and reload settings")
    logs = commands.add_parser("logs", help="Show recent background server messages")
    logs.add_argument("--lines", type=int, choices=range(1, 501), default=50, metavar="1-500")
    connect = commands.add_parser("connect", help="Register the server in an MCP client")
    connect.add_argument("client", choices=["codex"])
    connect.add_argument(
        "--transport", choices=["http", "stdio"], default="stdio", help="Default: stdio"
    )
    connect.add_argument(
        "--token-env",
        nargs="?",
        const=DEFAULT_TELEGRAM_TOKEN_ENV,
        metavar="NAME",
        help="Use a forwarded token variable and omit the service config (default name: %(const)s)",
    )
    client = commands.add_parser("client-config", help="Print ready-to-paste MCP settings")
    client.add_argument("client", choices=["codex", "json"], default="json", nargs="?")
    client.add_argument("--transport", choices=["http", "stdio"])
    client.add_argument(
        "--token-env",
        nargs="?",
        const=DEFAULT_TELEGRAM_TOKEN_ENV,
        metavar="NAME",
        help="Use a forwarded token variable and omit the service config (default name: %(const)s)",
    )
    commands.add_parser(
        "test", help="Send one real test notification through MCP and check delivery"
    )
    serve = commands.add_parser("serve", help="Run MCP and the durable delivery worker")
    serve.add_argument("--transport", choices=["http", "stdio"], default="http")
    serve.add_argument(
        "--token-env",
        metavar="NAME",
        help="Build a single Telegram channel from this environment variable",
    )
    serve.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("check-config", help="Validate settings and configured credentials")
    notify = commands.add_parser("notify", help="Queue an event locally; a running worker sends it")
    notify.add_argument("--message", required=True)
    notify.add_argument("--event", choices=list(Event), default=Event.INFO)
    notify.add_argument("--title", default="")
    notify.add_argument("--source", default="")
    notify.add_argument("--url")
    notify.add_argument("--idempotency-key")
    status = commands.add_parser("status", help="Show server state, or delivery state for an ID")
    status.add_argument("notification_id", nargs="?")
    return result


def configure_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # HTTP request URLs contain the Telegram bot token.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    arguments = parser().parse_args()
    configure_logging()
    try:
        config_path = resolve_config(arguments.config)
        if arguments.command == "setup":
            token = None
            if not config_path.exists() and not arguments.local and not arguments.import_config:
                token = getpass.getpass("Telegram bot token (input hidden): ").strip()
            created = setup_config(
                config_path,
                token=token,
                local=arguments.local,
                import_config=arguments.import_config,
            )
            print(f"{'Saved' if created else 'Already configured'}: {config_path}")
            if arguments.client == "codex":
                from notification_mcp.integrations import connect_codex

                settings = load_settings(config_path)
                target, changed = connect_codex(settings, config_path, arguments.transport)
                print(f"{'Connected' if changed else 'Already connected'}: {target}")
                if arguments.transport == "stdio":
                    print("Next: restart Codex; it will start and stop the server automatically.")
                    print("For Telegram: after restart, send /start to your bot.")
                    print("Then ask Codex to send a test notification using notify.")
                else:
                    print("Next: notification-mcp start, then restart Codex.")
                    print("For Telegram: send /start to your bot, then run notification-mcp test.")
            else:
                print("Next: notification-mcp start")
                print("For Telegram: send /start to your bot, then run notification-mcp test.")
            return 0
        if arguments.command in ("stop", "logs") or (
            arguments.command == "status" and arguments.notification_id is None
        ):
            from notification_mcp.runtime import Runtime

            runtime = Runtime(config_path)
            if arguments.command == "logs":
                print(runtime.logs(arguments.lines))
            else:
                state = runtime.stop() if arguments.command == "stop" else runtime.status()
                print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        token_env = getattr(arguments, "token_env", None)
        transport = getattr(arguments, "transport", None)
        if arguments.command == "client-config" and transport is None:
            transport = "stdio" if token_env is not None else "http"
        environment_client = (
            arguments.command in ("connect", "client-config") and token_env is not None
        )
        environment_server = (
            arguments.command == "serve"
            and transport == "stdio"
            and not arguments.managed
            and arguments.config is None
            and "NOTIFICATION_MCP_CONFIG" not in os.environ
            and (token_env is not None or DEFAULT_TELEGRAM_TOKEN_ENV in os.environ)
        )
        if environment_client:
            settings = None
        elif environment_server:
            settings = environment_settings(token_env or DEFAULT_TELEGRAM_TOKEN_ENV)
        else:
            settings = load_settings(
                config_path,
                check_secrets=arguments.command
                not in (
                    "status",
                    "connect",
                    "client-config",
                    "test",
                ),
            )
        if arguments.command in ("start", "restart"):
            from notification_mcp.runtime import Runtime

            runtime = Runtime(config_path)
            state = (
                runtime.start(settings)
                if arguments.command == "start"
                else runtime.restart(settings)
            )
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        if arguments.command in ("connect", "client-config"):
            from notification_mcp.integrations import client_config, connect_codex

            if arguments.command == "connect":
                target, changed = connect_codex(
                    settings,
                    None if environment_client else config_path,
                    transport,
                    token_env=token_env,
                )
                print(f"{'Connected' if changed else 'Already connected'}: {target}")
                if token_env is not None:
                    print(f"Codex will forward {token_env}; no service config is used.")
                if transport == "stdio":
                    print("Restart Codex; it will start and stop the server automatically.")
                else:
                    print("Run notification-mcp start, then restart Codex.")
                print("Ask Codex to send a notification using notify.")
            else:
                print(
                    client_config(
                        settings,
                        None if environment_client else config_path,
                        arguments.client,
                        transport,
                        token_env=token_env,
                    )
                )
            return 0
        if arguments.command == "test":
            from notification_mcp.diagnostics import check
            from notification_mcp.integrations import server_url

            try:
                asyncio.run(check(server_url(settings) + "/mcp", 30))
            except Exception:
                raise RuntimeError(
                    "Test did not confirm delivery. Run status and logs; check /start in Telegram. "
                    "If timed out, the notification may still be queued."
                ) from None
            return 0
        if arguments.command == "check-config":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "channels": {
                            name: channel.type for name, channel in settings.channels.items()
                        },
                    }
                )
            )
            return 0
        if arguments.command == "serve":
            if arguments.managed:
                if arguments.transport != "http":
                    raise ValueError("Background mode requires HTTP")
                from notification_mcp.runtime import run_managed

                run_managed(config_path, settings)
                return 0
            from notification_mcp.server import create_http_app, serve_stdio

            if arguments.transport == "stdio":
                asyncio.run(serve_stdio(settings))
            else:
                import uvicorn

                uvicorn.run(
                    create_http_app(settings),
                    host=settings.server.host,
                    port=settings.server.port,
                    log_config=None,
                    access_log=False,
                )
            return 0
        service = NotificationService(settings)
        if arguments.command == "notify":
            service.store.initialize()
            notification = Notification(
                message=arguments.message,
                event=arguments.event,
                title=arguments.title,
                source=arguments.source,
                url=arguments.url,
            )
            print(service.enqueue(notification, arguments.idempotency_key).model_dump_json())
        else:
            print(service.store.status(arguments.notification_id).model_dump_json())
        return 0
    except ValidationError as exc:
        # Exclude offending input values: a mistyped config can contain an inline secret.
        print(json.dumps(exc.errors(include_input=False, include_context=False)), file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(
            "Configuration not found. Run notification-mcp setup, pass --config PATH, "
            "or set TELEGRAM_BOT_TOKEN for config-free stdio.",
            file=sys.stderr,
        )
        return 2
    except EOFError:
        print("Setup needs an interactive terminal to enter the token.", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
