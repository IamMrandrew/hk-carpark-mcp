"""Shared search contract; fixtures do not test a model's intent classification."""

import asyncio
import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from hkparking.client import GovernmentClient, UpstreamError
from hkparking.metered import MeteredParkingService
from hkparking.search import ParkingSearchService
from hkparking.service import CatalogSnapshot, ParkingService, digest
from hkparking.tools import register_tools
from tests.test_metered import Gateway, space


class Catalog:
    def __init__(self, kind, count):
        self.calls = 0
        self.fail = False
        self.items = [
            {
                "park_Id": f"park{i:03}",
                "name_en": "Synthetic test parking",
                "name_zh": "測試停車場",
                "address_en": "Synthetic Road",
                "address_zh": "測試道",
                "district_en": "Synthetic district",
                "district_zh": "測試區",
                "opening_status": None,
            }
            if kind == "carpark"
            else space(f"{i + 1}A", District="Synthetic district", District_tc="測試區")
            for i in range(count)
        ]

    async def catalog(self):
        self.calls += 1
        if self.fail:
            raise UpstreamError("Synthetic selected source unavailable")
        return CatalogSnapshot(
            tuple(self.items),
            datetime(2026, 11, 3, tzinfo=UTC),
            0,
            digest(self.items),
            ("https://example.test/fixture",),
        )


def test_default_all_balanced_total_limit_and_full_pagination():
    async def run():
        carparks, meters = Catalog("carpark", 2), Catalog("metered", 7)
        service = ParkingSearchService(carparks, meters)
        page = await service.search_parking("測試", limit=1)
        assert page["parking_type"] == "all"
        assert page["totals"] == {"carpark": 2, "metered": 7}
        assert page["total"] == 9 and page["returned"] == 1
        items = list(page["items"])
        while page["has_more"]:
            page = await service.search_parking("測試", limit=3, cursor=page["next_cursor"])
            assert page["returned"] <= 3
            items.extend(page["items"])
        assert [r["parking_type"] for r in items] == ["carpark", "metered", "carpark"] + ["metered"] * 6
        assert len({(r["parking_type"], r.get("park_Id", r.get("ParkingSpaceId"))) for r in items}) == 9
        assert page["next_cursor"] is None
        balanced = await service.search_parking("測試", limit=4)
        assert [r["parking_type"] for r in balanced["items"]] == ["carpark", "metered"] * 2

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["carpark", "metered"])
def test_explicit_filter_fetches_only_selected_source(kind):
    async def run():
        carparks, meters = Catalog("carpark", 2), Catalog("metered", 7)
        other = meters if kind == "carpark" else carparks
        other.fail = True
        result = await ParkingSearchService(carparks, meters).search_parking(
            "synthetic", district="測試區", parking_type=kind, limit=50
        )
        assert all(row["parking_type"] == kind for row in result["items"])
        assert set(result["totals"]) == {kind}
        assert set(result["catalog_fetched_at"]) == {kind}
        assert result["total"] == (2 if kind == "carpark" else 7)
        assert other.calls == 0

    asyncio.run(run())


@pytest.mark.parametrize("empty_kind", ["carpark", "metered", "both"])
def test_empty_source_does_not_hide_other_matches(empty_kind):
    async def run():
        carparks = Catalog("carpark", 0 if empty_kind in ("carpark", "both") else 2)
        meters = Catalog("metered", 0 if empty_kind in ("metered", "both") else 7)
        result = await ParkingSearchService(carparks, meters).search_parking("synthetic", limit=50)
        assert result["total"] == len(carparks.items) + len(meters.items)
        assert not result["has_more"] and result["next_cursor"] is None
        assert result["totals"] == {"carpark": len(carparks.items), "metered": len(meters.items)}
        assert result["returned"] == len(result["items"])

    asyncio.run(run())


@pytest.mark.parametrize("failed_kind", ["carpark", "metered"])
def test_all_source_failure_is_an_error_not_partial_results(failed_kind):
    async def run():
        carparks, meters = Catalog("carpark", 2), Catalog("metered", 7)
        (carparks if failed_kind == "carpark" else meters).fail = True
        with pytest.raises(UpstreamError):
            await ParkingSearchService(carparks, meters).search_parking("synthetic")

    asyncio.run(run())


def test_cursor_binds_query_district_type_and_both_snapshots():
    async def run():
        carparks, meters = Catalog("carpark", 2), Catalog("metered", 7)
        service = ParkingSearchService(carparks, meters)
        first = await service.search_parking("synthetic", limit=2)
        cursor = first["next_cursor"]
        for kwargs in (
            {"query": "測試"},
            {"query": "synthetic", "district": "測試區"},
            {"query": "synthetic", "parking_type": "metered"},
            {"query": "synthetic", "cursor": "invalid!"},
        ):
            args = {"cursor": cursor} | kwargs
            with pytest.raises(ValueError, match="cursor"):
                await service.search_parking(**args)
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for offset in (True, -1, 999):
            bad = base64.urlsafe_b64encode(json.dumps(payload | {"offset": offset}).encode()).decode()
            with pytest.raises(ValueError, match="cursor"):
                await service.search_parking("synthetic", cursor=bad)
        for source in (carparks, meters):
            original = source.items[:]
            source.items.pop()
            with pytest.raises(ValueError, match="cursor"):
                await service.search_parking("synthetic", cursor=cursor)
            source.items = original

    asyncio.run(run())


def test_invalid_shared_search_input_does_not_fetch():
    async def run():
        carparks, meters = Catalog("carpark", 2), Catalog("metered", 7)
        service = ParkingSearchService(carparks, meters)
        for kwargs in (
            {"query": " "},
            {"query": "x" * 121},
            {"district": " "},
            {"parking_type": "meter"},
            {"parking_type": None},
            {"limit": True},
            {"limit": 0},
            {"limit": 51},
            {"cursor": ""},
            {"cursor": "x" * 1025},
        ):
            with pytest.raises(ValueError):
                await service.search_parking(**({"query": "synthetic"} | kwargs))
        assert carparks.calls == meters.calls == 0

    asyncio.run(run())


def test_mcp_schema_description_and_default_parameter_handling():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
        ) as http:
            mcp = FastMCP("Synthetic shared search")
            register_tools(mcp, ParkingService(GovernmentClient(http)), MeteredParkingService(Gateway()))
            listing = {tool.name: tool for tool in await mcp.list_tools()}
            tool = listing["search_parking"]
            parameter = tool.inputSchema["properties"]["parking_type"]
            assert parameter["default"] == "all"
            assert parameter["enum"] == ["carpark", "metered", "all"]
            assert "parking_type" not in tool.inputSchema["required"]
            for phrase in ("停車場", "car park", "咪錶位", "咪錶停車場", "附近有啲咩停車嘅地方", "TOTAL"):
                assert phrase in tool.description
            assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
            assert "Compatibility" in listing["search_carparks"].description
            assert "Compatibility" in listing["search_metered_spaces"].description
            _, result = await mcp.call_tool("search_parking", {"query": "測試"})
            assert result["parking_type"] == "all" and result["totals"]["carpark"] == 0
            _, filtered = await mcp.call_tool("search_parking", {"query": "測試", "parking_type": "carpark"})
            assert filtered["items"] == [] and filtered["parking_type"] == "carpark"
            with pytest.raises(Exception, match="parking_type"):
                await mcp.call_tool("search_parking", {"query": "測試", "parking_type": "meter"})

    asyncio.run(run())
