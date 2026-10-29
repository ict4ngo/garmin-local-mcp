"""Static Bearer-token protection for the Streamable HTTP MCP transport."""

from __future__ import annotations

import contextlib
import hmac
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerTokenMiddleware:
    """Require one configured Bearer token on the MCP endpoint only."""

    def __init__(self, app: ASGIApp, expected_token: str, path: str) -> None:
        if not expected_token:
            raise ValueError("expected_token must not be empty")
        self.app = app
        self.expected_token = expected_token.encode("utf-8")
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == self.path:
            values = [
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ]
            parts = values[0].split(None, 1) if len(values) == 1 else []
            valid = (
                len(parts) == 2
                and parts[0].lower() == b"bearer"
                and hmac.compare_digest(parts[1], self.expected_token)
            )
            if not valid:
                response = PlainTextResponse("Unauthorized", status_code=401)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


def create_http_app(mcp_server: FastMCP, expected_token: str) -> Starlette:
    """Mount the MCP Streamable HTTP endpoint behind a static Bearer token."""
    if not expected_token:
        raise ValueError("expected_token must not be empty")

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield

    mcp_app = mcp_server.streamable_http_app()
    protected_mcp_app = BearerTokenMiddleware(
        mcp_app,
        expected_token,
        mcp_server.settings.streamable_http_path,
    )
    return Starlette(routes=[Mount("/", app=protected_mcp_app)], lifespan=lifespan)
