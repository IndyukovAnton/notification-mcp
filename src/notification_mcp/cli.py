import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from notification_mcp.config import load_settings
from notification_mcp.models import Event, Notification
from notification_mcp.service import NotificationService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local, channel-independent notification service")
    result.add_argument("--config", type=Path, default=Path("config.toml"))
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run MCP and the durable delivery worker")
    serve.add_argument("--transport", choices=["http", "stdio"], default="http")
    commands.add_parser("check-config", help="Validate settings and configured credentials")
    notify = commands.add_parser("notify", help="Queue an event locally; a running worker sends it")
    notify.add_argument("--message", required=True)
    notify.add_argument("--event", choices=list(Event), default=Event.INFO)
    notify.add_argument("--title", default="")
    notify.add_argument("--source", default="")
    notify.add_argument("--url")
    notify.add_argument("--idempotency-key")
    status = commands.add_parser("status", help="Read delivery status from the local queue")
    status.add_argument("notification_id")
    return result


def configure_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # HTTP request URLs contain the Telegram bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    arguments = parser().parse_args()
    configure_logging()
    try:
        settings = load_settings(arguments.config, check_secrets=arguments.command != "status")
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
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
