from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from notification_mcp.config import Settings
from notification_mcp.models import EnqueueResult, Event, Notification, NotificationStatus
from notification_mcp.service import NotificationService


def create_mcp(service: NotificationService) -> FastMCP:
    port = service.settings.server.port
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    mcp = FastMCP(
        "Notifications",
        instructions=(
            "Notify the user when a decision is needed, work is ready for review, or an error "
            "requires attention. Channels and recipients are configured by the server. "
            "A queued result means durably accepted, not delivered or read. "
            "Use notification_status to check delivery. Reuse an idempotency_key for the same "
            "logical event; use a new key for a new event. Do not include credentials in messages."
        ),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"http://{host}" for host in hosts],
        ),
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        )
    )
    async def notify(
        message: str,
        event: Event = Event.INFO,
        title: str = "",
        source: str = "",
        url: str | None = None,
        idempotency_key: str | None = None,
    ) -> EnqueueResult:
        """Queue a notification. Delivery channel and recipient are chosen by server settings.

        event: info, action_required (needs a decision), review_requested (ready to inspect),
        or error. source identifies the project/task. url optionally links to the result.
        idempotency_key prevents duplicate queue entries for an identical logical event.
        """
        if not service.healthy:
            raise ValueError("Delivery worker is unavailable; restart the server")
        notification = Notification(
            message=message, event=event, title=title, source=source, url=url
        )
        return service.enqueue(notification, idempotency_key)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def notification_status(notification_id: str) -> NotificationStatus:
        """Read queued/sending/sent/failed status. 'sent' does not confirm the user read it."""
        return service.store.status(notification_id)

    return mcp


def create_http_app(settings: Settings) -> Starlette:
    service = NotificationService(settings)
    mcp = create_mcp(service)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with service.running(), mcp.session_manager.run():
            yield

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok" if service.healthy else "unavailable"},
            status_code=200 if service.healthy else 503,
        )

    return Starlette(routes=[Route("/health", health), Mount("/", app=mcp_app)], lifespan=lifespan)


async def serve_stdio(settings: Settings) -> None:
    service = NotificationService(settings)
    async with service.running():
        await create_mcp(service).run_stdio_async()
