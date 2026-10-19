"""Authenticated real-socket MCP search integration with synthetic upstream fixtures."""

import asyncio
import importlib.util
import secrets
import socket
import threading
import time

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

from hkparking.client import GovernmentClient
from hkparking.service import ParkingService


def test_authenticated_mcp_search_flow():
    assert importlib.util.find_spec("hkparking.tools"), "Parking MCP tools not implemented"
    from hkparking.tools import register_tools

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    token = secrets.token_urlsafe(32)

    class Verifier(TokenVerifier):
        async def verify_token(self, candidate):
            return (
                AccessToken(
                    token=candidate,
                    client_id="synthetic-test-client",
                    scopes=["parking:read"],
                    expires_at=int(time.time()) + 60,
                    resource=base + "/mcp",
                )
                if candidate == token
                else None
            )

    gateway_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "park_Id": "synthetic1",
                            "name": "Synthetic 荃灣 Park",
                            "displayAddress": "Synthetic address",
                            "district": "Synthetic 荃灣",
                        }
                    ]
                },
            )
        )
    )
    service = ParkingService(GovernmentClient(gateway_http))
    mcp = FastMCP(
        "Synthetic parking test",
        host="127.0.0.1",
        stateless_http=True,
        json_response=True,
        token_verifier=Verifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(base),
            resource_server_url=AnyHttpUrl(base + "/mcp"),
            validate_token_resource=True,
            required_scopes=["parking:read"],
        ),
    )
    register_tools(mcp, service)
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), access_log=False, log_level="error"))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()

    async def check():
        async with (
            httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http,
            streamable_http_client(base + "/mcp", http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"search_carparks", "get_carpark_vacancies"}
            assert all(t.annotations.readOnlyHint for t in tools.tools)
            assert all(t.annotations.destructiveHint is False for t in tools.tools)
            result = await session.call_tool("search_carparks", {"query": "荃灣"})
            assert not result.isError
            assert result.structuredContent["total"] == 1
            assert result.structuredContent["items"][0]["park_Id"] == "synthetic1"
            invalid = await session.call_tool("search_carparks", {"query": "荃灣", "limit": 500})
            assert invalid.isError

    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert worker.is_alive() and time.monotonic() < deadline
            threading.Event().wait(0.01)
        asyncio.run(check())
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        asyncio.run(gateway_http.aclose())
        assert not worker.is_alive()
