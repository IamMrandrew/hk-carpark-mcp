"""Production resource-server boundary; all upstream/token data here are labelled fixtures."""

import asyncio
import importlib.util
import time

import httpx
import pytest
from mcp.server.auth.provider import AccessToken, TokenVerifier

from hkparking.config import ServerConfig

PUBLIC = "https://carpark.example.test"
RESOURCE = PUBLIC + "/mcp"
ISSUER = "https://synthetic-issuer.example/oauth"


class FixtureVerifier(TokenVerifier):
    """Synthetic test tokens, never production credentials or an authorization server."""

    async def verify_token(self, token):
        if token in {
            "synthetic-valid",
            "synthetic-expired",
            "synthetic-wrong-scope",
            "synthetic-wrong-resource",
            "synthetic-missing-resource",
        }:
            return AccessToken(
                token=token,
                client_id="synthetic-client",
                scopes=[] if token == "synthetic-wrong-scope" else ["parking:read"],
                expires_at=int(time.time()) + (-60 if token == "synthetic-expired" else 60),
                resource=(
                    None
                    if token == "synthetic-missing-resource"
                    else "https://wrong-resource.example/mcp"
                    if token == "synthetic-wrong-resource"
                    else RESOURCE
                ),
            )
        return None


def factory():
    assert importlib.util.find_spec("hkparking.app"), "Production parking app factory is missing"
    from hkparking.app import build_app

    return build_app


def no_upstream(request):
    raise AssertionError("Auth/discovery must not contact the government API")


def test_public_discovery_and_unauthenticated_challenge():
    app = factory()(
        ServerConfig(issuer_url=ISSUER),
        FixtureVerifier(),
        upstream_transport=httpx.MockTransport(no_upstream),
    )

    async def check():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as http,
        ):
            response = await http.post("/mcp", json={})
            assert response.status_code == 401
            assert (
                'resource_metadata="' + PUBLIC + '/.well-known/oauth-protected-resource/mcp"'
                in (response.headers["www-authenticate"])
            )
            metadata = await http.get("/.well-known/oauth-protected-resource/mcp")
            assert metadata.status_code == 200
            assert metadata.json()["resource"] == RESOURCE
            assert metadata.json()["authorization_servers"] == [ISSUER]
            assert metadata.json()["scopes_supported"] == ["parking:read"]
            for route in ("/authorize", "/token", "/register"):
                assert (await http.get(route)).status_code == 404

    asyncio.run(check())


@pytest.mark.parametrize("verifier", [None, object(), False])
def test_verifier_cannot_be_missing_or_invalid(verifier):
    with pytest.raises(TypeError, match="token_verifier"):
        factory()(ServerConfig(issuer_url=ISSUER), verifier)


def test_verifier_has_no_default():
    with pytest.raises(TypeError, match="token_verifier"):
        factory()(ServerConfig(issuer_url=ISSUER))


@pytest.mark.parametrize("path", ["/mcp", "/.well-known/oauth-protected-resource/mcp"])
@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"host": "127.0.0.1:8767"}, 421),
        ({"host": "carpark.example.test:9999"}, 421),
        ({"host": "carpark.example.test.evil.example"}, 421),
        ({"host": "evil.example", "x-forwarded-host": "carpark.example.test"}, 421),
        ({"origin": "https://evil.example"}, 403),
        ({"origin": "http://carpark.example.test"}, 403),
        ({"origin": "https://carpark.example.test:444"}, 403),
        ({"origin": "null"}, 403),
        ({"origin": ""}, 403),
        ([("host", "carpark.example.test"), ("host", "evil.example")], 421),
        ([("origin", PUBLIC), ("origin", "https://evil.example")], 403),
    ],
)
def test_public_boundary_rejects_untrusted_headers_before_auth(path, headers, expected):
    app = factory()(
        ServerConfig(issuer_url=ISSUER),
        FixtureVerifier(),
        upstream_transport=httpx.MockTransport(no_upstream),
    )

    async def check():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as http,
        ):
            response = await http.get(path, headers=headers)
            assert response.status_code == expected

    asyncio.run(check())


@pytest.mark.parametrize("origin", [None, PUBLIC])
def test_public_boundary_accepts_exact_origin_or_non_browser_client(origin):
    app = factory()(
        ServerConfig(issuer_url=ISSUER),
        FixtureVerifier(),
        upstream_transport=httpx.MockTransport(no_upstream),
    )

    async def check():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as http,
        ):
            headers = {} if origin is None else {"origin": origin}
            response = await http.get("/.well-known/oauth-protected-resource/mcp", headers=headers)
            assert response.status_code == 200

    asyncio.run(check())


@pytest.mark.parametrize(
    "token, status",
    [
        ("synthetic-unknown", 401),
        ("synthetic-expired", 401),
        ("synthetic-wrong-resource", 401),
        ("synthetic-missing-resource", 401),
        ("synthetic-wrong-scope", 403),
    ],
)
def test_rejects_invalid_expired_unbound_or_insufficient_tokens(token, status):
    app = factory()(
        ServerConfig(issuer_url=ISSUER),
        FixtureVerifier(),
        upstream_transport=httpx.MockTransport(no_upstream),
    )

    async def check():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as http,
        ):
            response = await http.post("/mcp", json={}, headers={"authorization": "Bearer " + token})
        assert response.status_code == status
        assert "resource_metadata=" in response.headers["www-authenticate"]

    asyncio.run(check())


class FixtureGovernmentTransport(httpx.AsyncBaseTransport):
    """Synthetic government records with a deliberately historical update, never live data."""

    def __init__(self):
        self.requests = []
        self.request_loops = []
        self.close_loops = []

    async def handle_async_request(self, request):
        from hkparking.client import ENDPOINT

        assert not self.close_loops, "Shared upstream transport was closed per MCP request"
        assert str(request.url).split("?")[0] == ENDPOINT
        assert request.method == "GET"
        self.requests.append(request)
        self.request_loops.append(asyncio.get_running_loop())
        if request.url.params["data"] == "info":
            record = {
                "park_Id": "synthetic1",
                "name": "Synthetic Fixture Park",
                "displayAddress": "Synthetic address",
                "district": "Synthetic district",
            }
        else:
            assert request.url.params["carparkIds"] == "synthetic1"
            assert request.url.params["vehicleTypes"] == "privateCar"
            record = {
                "park_Id": "synthetic1",
                "privateCar": [
                    {
                        "vehicle_type": "privateCar",
                        "category": "HOURLY",
                        "vacancy_type": "A",
                        "vacancy": 7,
                        "lastupdate": "2000-01-01 12:00:00",
                    }
                ],
            }
        return httpx.Response(200, json={"results": [record]})

    async def aclose(self):
        self.close_loops.append(asyncio.get_running_loop())


async def rpc(http, method, params=None):
    response = await http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        },
        headers={"accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-11-25"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    return response.json()["result"]


def test_authenticated_tools_share_upstream_until_serving_loop_shutdown():
    upstream = FixtureGovernmentTransport()
    app = factory()(ServerConfig(issuer_url=ISSUER), FixtureVerifier(), upstream_transport=upstream)
    assert not upstream.requests, "Building the app must not fetch upstream data"

    async def check():
        serving_loop = asyncio.get_running_loop()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=PUBLIC,
                headers={"authorization": "Bearer synthetic-valid"},
            ) as http,
        ):
            initialized = await rpc(
                http,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-client", "version": "1"},
                },
            )
            assert initialized["serverInfo"]["name"] == "Carpark"
            listing = await rpc(http, "tools/list")
            assert {tool["name"] for tool in listing["tools"]} == {
                "search_carparks",
                "get_carpark_vacancies",
            }
            assert all(tool["annotations"]["readOnlyHint"] for tool in listing["tools"])
            assert all(not tool["annotations"]["destructiveHint"] for tool in listing["tools"])
            search = await rpc(
                http, "tools/call", {"name": "search_carparks", "arguments": {"query": "Synthetic"}}
            )
            assert not search["isError"]
            assert search["structuredContent"]["items"][0]["park_Id"] == "synthetic1"
            assert search["structuredContent"]["total"] == 1
            vacancy = await rpc(
                http,
                "tools/call",
                {"name": "get_carpark_vacancies", "arguments": {"carpark_ids": ["synthetic1"]}},
            )
            assert not vacancy["isError"]
            records = vacancy["structuredContent"]["items"][0]["records"]
            assert records[0]["reported_count"] == 7
            assert records[0]["current_count"] is None  # Historical fixture, not live availability.
            invalid = await rpc(
                http,
                "tools/call",
                {"name": "search_carparks", "arguments": {"query": "Synthetic", "limit": 500}},
            )
            assert invalid["isError"]
            assert len(upstream.requests) == 3  # Shared bilingual catalog plus vacancy request.
            assert not upstream.close_loops
        assert upstream.close_loops == [serving_loop]
        assert all(loop is serving_loop for loop in upstream.request_loops)

    asyncio.run(check())


@pytest.mark.parametrize("body_mode", ["content-length", "chunked", "underreported-length"])
def test_request_body_above_64_kib_is_rejected(body_mode):
    app = factory()(
        ServerConfig(issuer_url=ISSUER),
        FixtureVerifier(),
        upstream_transport=httpx.MockTransport(no_upstream),
    )

    async def check():
        headers = {
            "authorization": "Bearer synthetic-valid",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if body_mode == "underreported-length":
            headers["content-length"] = "1"

        async def chunks():
            yield b" " * 32768
            yield b" " * 32768
            yield b" "

        content = b" " * 65537 if body_mode == "content-length" else chunks()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as http,
        ):
            response = await http.post("/mcp", content=content, headers=headers)
        assert response.status_code == 413

    asyncio.run(check())


def test_request_body_at_64_kib_is_accepted():
    app = factory()(
        ServerConfig(issuer_url=ISSUER),
        FixtureVerifier(),
        upstream_transport=httpx.MockTransport(no_upstream),
    )

    async def check():
        headers = {
            "authorization": "Bearer synthetic-valid",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
        body += b" " * (65536 - len(body))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as http,
        ):
            response = await http.post("/mcp", content=body, headers=headers)
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 2

    asyncio.run(check())


def test_runtime_binds_only_loopback_without_trusting_proxy_headers(monkeypatch):
    import hkparking.app as module

    assert hasattr(module, "run_app"), "A loopback-bound runtime entry point is missing"
    captured = {}

    def inspect_runtime(app, **options):
        captured.update(options)

        # Exercise the actual returned ASGI app, not a fake production verifier/client.
        async def check():
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=PUBLIC) as http,
            ):
                response = await http.get("/mcp")
            assert response.status_code == 401

        asyncio.run(check())

    monkeypatch.setattr("uvicorn.run", inspect_runtime)
    module.run_app(ServerConfig(issuer_url=ISSUER, listen_port=9876), FixtureVerifier())
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9876
    assert captured["proxy_headers"] is False
    assert captured["lifespan"] == "on"


def test_official_sdk_client_over_real_loopback_socket():
    import socket

    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    upstream = FixtureGovernmentTransport()
    app = factory()(ServerConfig(issuer_url=ISSUER), FixtureVerifier(), upstream_transport=upstream)

    async def check():
        serving_loop = asyncio.get_running_loop()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = listener.getsockname()
        assert address[0] == "127.0.0.1"
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", proxy_headers=False, lifespan="on", access_log=False, log_level="error"
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    assert not task.done(), "Socket server failed to start"
                    await asyncio.sleep(0.01)
            base = f"http://127.0.0.1:{address[1]}"
            headers = {
                "Host": "carpark.example.test",
                "Origin": PUBLIC,
                "Authorization": "Bearer synthetic-valid",
            }
            async with (
                httpx.AsyncClient(headers=headers) as http,
                streamable_http_client(base + "/mcp", http_client=http) as (read, write, session_id),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                listing = await session.list_tools()
                assert {tool.name for tool in listing.tools} == {"search_carparks", "get_carpark_vacancies"}
                found = await session.call_tool("search_carparks", {"query": "Synthetic"})
                assert not found.isError
                assert found.structuredContent["items"][0]["park_Id"] == "synthetic1"
                vacancies = await session.call_tool("get_carpark_vacancies", {"carpark_ids": ["synthetic1"]})
                assert not vacancies.isError
                assert vacancies.structuredContent["items"][0]["records"][0]["current_count"] is None
                assert session_id() is None
                assert not upstream.close_loops
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)
            listener.close()
        assert upstream.close_loops == [serving_loop]
        assert all(loop is serving_loop for loop in upstream.request_loops)

    asyncio.run(check())
