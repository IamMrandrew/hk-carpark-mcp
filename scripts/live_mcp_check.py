"""REAL authenticated loopback MCP → government API smoke. No fabricated responses.

No public listener, tunnel, OAuth grant or persistent token; generated bearer stays in memory.
Run with: uv run python scripts/live_mcp_check.py
For a proxy-managed development VM, add --use-env-proxy (upstream HTTP only).
"""

import argparse
import asyncio
import json
import secrets
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from hkparking.client import GovernmentClient
from hkparking.combined_carparks import CombinedCarparkService
from hkparking.metered import MeteredClient, MeteredParkingService
from hkparking.service import ParkingService
from hkparking.tools import register_tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--use-env-proxy", action="store_true",
        help="Honor environment proxy/TLS settings for government requests in this smoke test only.",
    )
    parser.add_argument("--hkemobility", action="store_true", help="Also test experimental WFS MCP tool")
    args = parser.parse_args()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    token = secrets.token_urlsafe(48)
    expiry = int(time.time()) + 600

    class Verifier(TokenVerifier):
        async def verify_token(self, candidate):
            if not secrets.compare_digest(candidate, token):
                return None
            return AccessToken(
                token=candidate,
                client_id="local-live-verification",
                scopes=["parking:read"],
                expires_at=expiry,
                resource=base + "/mcp",
            )

    # Never reuse a Grok/MCP-auth client for upstream requests.
    upstream_http = httpx.AsyncClient(trust_env=args.use_env_proxy, follow_redirects=False, timeout=10)
    service = ParkingService(GovernmentClient(upstream_http))
    if args.hkemobility:
        service = CombinedCarparkService(service)
    mcp = FastMCP(
        "Parking live verification",
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
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[base.removeprefix("http://")],
            allowed_origins=[base],
        ),
    )
    register_tools(mcp, service, MeteredParkingService(MeteredClient(upstream_http)))
    app = mcp.streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with original_lifespan(instance):
            try:
                yield
            finally:
                await upstream_http.aclose()

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, proxy_headers=False))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()

    async def check():
        async with httpx.AsyncClient(trust_env=False) as anonymous:
            response = await anonymous.post(
                base + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "unauthorized-check", "version": "1"},
                    },
                },
            )
            assert response.status_code == 401
        async with (
            httpx.AsyncClient(
                trust_env=False, timeout=30, headers={"Authorization": "Bearer " + token}
            ) as authorized,
            streamable_http_client(base + "/mcp", http_client=authorized) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "search_carparks", "get_carpark_vacancies", "search_metered_spaces", "get_metered_occupancy", "search_parking"
            }

            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, f"Tool failed: {name}: {result.content}"
                assert result.structuredContent is not None
                return result.structuredContent

            if args.hkemobility:
                for query in ("Globe", "Globe", "Tsuen Wan Plaza", "荃灣", "The One"):
                    found = await call("search_parking", {"query": query, "parking_type": "carpark"})
                    assert found["items"], f"Combined search empty: {query}"
                    assert all("source_refs" in item and item["park_Id"] for item in found["items"])
                    print(json.dumps({"event": "COMBINED_SEARCH_PASSED", "query": query,
                                      "result": found}, ensure_ascii=False))
                    if query in ("Globe", "The One"):
                        chosen_source = next((v for v in found["items"]
                            if v["source_refs"].get("hkemobility", {}).get("park_id")
                            == ("911202" if query == "Globe" else "10015")), None)
                        assert chosen_source is not None
                        detail = await call("get_carpark_vacancies", {"carpark_ids": [chosen_source["park_Id"]]})
                        item = detail["items"][0]
                        assert item["status"] not in ("unavailable", "not_found")
                        assert "hkemobility" in item["source_results"]
                        assert all(request["status"] == 200 for request in item["source_results"]["hkemobility"]["requests"])
                        if query == "Globe":
                            assert item["records"] and all(r["current_count"] is None for r in item["records"])
                        print(json.dumps({"event": "COMBINED_VACANCY_PASSED", "query": query,
                                          "result": detail}, ensure_ascii=False))
                plaza = await call("get_carpark_vacancies", {"carpark_ids": ["tdc327p1"]})
                assert plaza["items"][0]["source_status"] == {"government": "ok", "hkemobility": "ok"}
                assert plaza["items"][0]["conflicts"]
                assert all(r["current_count"] is None for r in plaza["items"][0]["records"])
                mixed = await call("get_carpark_vacancies", {"carpark_ids": ["hkemobility--911202", "tdc327p1", "100"]})
                assert len(mixed["items"]) == 3
                assert mixed["items"][2]["source_status"] == {"government": "ok"}
                print(json.dumps({"event": "MIXED_SOURCE_BATCH_PASSED", "result": mixed}, ensure_ascii=False))
                print(json.dumps({"event": "COMBINED_VACANCY_PASSED", "query": "Plaza", "result": plaza},
                                 ensure_ascii=False))

            shared = await call("search_parking", {"query": "Tsuen Wan", "limit": 2})
            assert [row["parking_type"] for row in shared["items"]] == ["carpark", "metered"]
            exact_colloquial = await call("search_carparks", {"query": "荃灣多層停車場"})
            broad = await call("search_carparks", {"query": "荃灣", "limit": 2})
            items = list(broad["items"])
            page = broad
            while page["has_more"]:
                page = await call(
                    "search_carparks", {"query": "荃灣", "limit": 2, "cursor": page["next_cursor"]}
                )
                items.extend(page["items"])
            assert len(items) == broad["total"] == len({p.get("combined_id", p["park_Id"]) for p in items})
            # Explicit HUMAN/test-script selection, not server semantic resolution.
            chosen = next(p for p in items if p["park_Id"] == "tdcp3")
            assert chosen["name_zh"] == "荃灣停車場"
            english = await call("search_carparks", {"query": "Tsuen Wan Car Park"})
            english_items = list(english["items"])
            english_page = english
            while english_page["has_more"]:
                english_page = await call(
                    "search_carparks",
                    {"query": "Tsuen Wan Car Park", "cursor": english_page["next_cursor"]},
                )
                english_items.extend(english_page["items"])
            assert len(english_items) == english["total"]
            assert any(p["park_Id"] == chosen["park_Id"] for p in english_items)
            vacancies = await call("get_carpark_vacancies", {"carpark_ids": [chosen["park_Id"]]})
            assert vacancies["source_url"].startswith("https://api.data.gov.hk/v1/carpark-info-vacancy?")
            assert vacancies["items"][0]["park_Id"] == chosen["park_Id"]
            invalid = await session.call_tool("get_carpark_vacancies", {"carpark_ids": ["http://untrusted/"]})
            assert invalid.isError
            missing = await call("get_carpark_vacancies", {"carpark_ids": ["NONEXISTENT_SYNTHETIC_ID"]})
            assert missing["items"][0]["status"] == "not_found"
            meters = await call("search_metered_spaces", {"query": "ISLAND ROAD", "limit": 2})
            assert meters["items"], "Official meter search returned no matches"
            meter_ids = [row["ParkingSpaceId"] for row in meters["items"]]
            occupancy = await call("get_metered_occupancy", {"parking_space_ids": meter_ids})
            assert all(row["status"] != "not_found" for row in occupancy["items"])
            assert all(
                row["record"] is None or row["record"]["current_status"] == "unknown"
                for row in occupancy["items"]
            )
            print(
                json.dumps(
                    {
                        "event": "LIVE_MCP_CHECK_PASSED",
                        "verified_at": datetime.now(UTC).isoformat(),
                        "upstream_environment_settings": args.use_env_proxy,
                        "auth": "401 without token; authenticated SDK initialize/list/call succeeded",
                        "shared_search_totals": shared["totals"],
                        "metered_search_total": meters["total"],
                        "metered_occupancy": occupancy,
                        "input_validation": "arbitrary URL rejected",
                        "colloquial_exact_search_total": exact_colloquial["total"],
                        "broad_search_total": broad["total"],
                        "all_pages_enumerated": len(items),
                        "chosen_official_carpark": chosen,
                        "vacancy": vacancies,
                        "selection_note": "Test-script selected tdcp3; Grok name resolution is NOT verified here.",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert worker.is_alive() and time.monotonic() < deadline, "Loopback server startup failed"
            threading.Event().wait(0.01)
        asyncio.run(check())
    finally:
        server.should_exit = True
        worker.join(timeout=15)
        listener.close()
        assert not worker.is_alive(), "Test listener failed to stop"


if __name__ == "__main__":
    main()
