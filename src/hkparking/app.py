"""Parking MCP resource server. The caller supplies a production token verifier."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import uvicorn
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .client import GovernmentClient
from .config import ServerConfig
from .deployment import SETTINGS
from .service import ParkingService
from .tools import register_tools

MAX_REQUEST_BODY_BYTES = 64 * 1024


class _PublicBoundary:
    """Check all HTTP routes before auth; never trust forwarded Host/Origin values."""

    def __init__(self, app: ASGIApp, host: str, origin: str):
        self.app = app
        self.host = host.encode("ascii")
        self.origin = origin.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            hosts = [value for key, value in scope["headers"] if key.lower() == b"host"]
            origins = [value for key, value in scope["headers"] if key.lower() == b"origin"]
            if hosts != [self.host]:
                await PlainTextResponse("Invalid Host header", status_code=421)(scope, receive, send)
                return
            if origins and origins != [self.origin]:
                await PlainTextResponse("Invalid Origin header", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(
    config: ServerConfig,
    token_verifier: TokenVerifier,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """Build the resource server; upstream_transport is explicit synthetic-test injection."""
    if token_verifier is None or not callable(getattr(token_verifier, "verify_token", None)):
        raise TypeError("token_verifier must be an explicitly supplied TokenVerifier")
    public_host = urlsplit(config.public_origin).netloc
    http = httpx.AsyncClient(transport=upstream_transport, follow_redirects=False, trust_env=False)
    mcp = FastMCP(
        SETTINGS.display_name,
        host=config.listen_host,
        port=config.listen_port,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        token_verifier=token_verifier,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host],
            allowed_origins=[config.public_origin],
        ),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(config.issuer_url),
            resource_server_url=AnyHttpUrl(config.resource_url),
            required_scopes=["parking:read"],
            validate_token_resource=True,
        ),
    )
    register_tools(mcp, ParkingService(GovernmentClient(http)))
    app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # The SDK's per-request lifespan is not the owner in stateless HTTP mode.
        # Exit the serving manager before closing its shared upstream on this loop.
        async with http, mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    app.add_middleware(_PublicBoundary, host=public_host, origin=config.public_origin)
    return app


def run_app(config: ServerConfig, token_verifier: TokenVerifier) -> None:
    """Serve behind the approved tunnel only; authorization-server integration is external."""
    uvicorn.run(
        build_app(config, token_verifier),
        host=config.listen_host,
        port=config.listen_port,
        proxy_headers=False,
        lifespan="on",
    )
