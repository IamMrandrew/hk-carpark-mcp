"""Synthetic CSV fixtures, never represented as live parking observations."""

import asyncio
import csv
import io
from datetime import UTC, datetime

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from hkparking.client import GovernmentClient, UpstreamError, UpstreamResult
from hkparking.metered import (
    INFO_FIELDS,
    OCCUPANCY_FIELDS,
    OCCUPANCY_URL,
    SPACE_INFO_URL,
    MeteredClient,
    MeteredParkingService,
    interpret_occupancy,
    parse_csv,
)
from hkparking.service import ParkingService
from hkparking.tools import register_tools


def space(space_id="123A", **overrides):
    return (
        dict.fromkeys(INFO_FIELDS, "")
        | {
            "PoleId": space_id[:-1],
            "ParkingSpaceId": space_id,
            "Region": "KOWLOON",
            "District": "YAU TSIM MONG",
            "District_tc": "油尖旺",
            "Street": "SYNTHETIC ROAD",
            "Street_tc": "測試道",
            "Latitude": "22.3",
            "Longitude": "114.2",
            "VehicleType": "A",
            "LPP": "120",
            "OperatingPeriod": "D",
            "TimeUnit": "15",
            "PaymentUnit": "4.00",
        }
        | overrides
    )


def occupancy(space_id="123A", **overrides):
    return {
        "ParkingSpaceId": space_id,
        "ParkingMeterStatus": "N",
        "OccupancyStatus": "V",
        "OccupancyDateChanged": "01/20/2021 01:00:00 PM",
    } | overrides


def csv_bytes(rows, kind="info", preamble=True):
    out = io.StringIO()
    writer = csv.writer(out)
    fields = INFO_FIELDS if kind == "info" else OCCUPANCY_FIELDS
    if kind == "info" and preamble:
        writer.writerow(["2026-11-02"])
        writer.writerow([""] * len(fields))
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row[k] for k in fields])
    return out.getvalue().encode("utf-8-sig")


class Gateway:
    def __init__(self):
        self.rows = [space("123B"), space(), space("124A", Street="OTHER ROAD", Street_tc="其他道")]
        self.occupancies = [occupancy(), occupancy("123B", ParkingMeterStatus="NU")]
        self.calls = []
        self.fail = False

    async def fetch(self, kind):
        self.calls.append(kind)
        if self.fail:
            raise UpstreamError("Synthetic unavailable")
        return UpstreamResult(
            self.rows if kind == "info" else self.occupancies,
            SPACE_INFO_URL if kind == "info" else OCCUPANCY_URL,
            datetime(2026, 11, 3, tzinfo=UTC),
        )


def test_csv_preamble_bom_quotes_and_internal_test_exclusion():
    row = space(Street='SYNTHETIC, "QUOTED" ROAD')
    assert parse_csv(csv_bytes([row, space("90001A")]), "info") == [row]
    assert parse_csv(csv_bytes([row], preamble=False), "info") == [row]
    assert parse_csv(csv_bytes([occupancy()], "occupancy"), "occupancy") == [occupancy()]


@pytest.mark.parametrize(
    "data",
    [
        b"bad",
        b"\xff",
        csv_bytes([space(), space()]),
        csv_bytes([space(ParkingSpaceId="bad/id")]),
        csv_bytes([space(Latitude="nan")]),
        csv_bytes([space(Longitude="181")]),
        csv_bytes([space(PoleId="not-a-number")]),
        csv_bytes([space()]) + b"one,two\r\n",
        b'ParkingSpaceId,ParkingMeterStatus,OccupancyStatus,OccupancyDateChanged\r\n"unclosed',
    ],
)
def test_bad_csv_fails_closed(data):
    with pytest.raises(UpstreamError):
        parse_csv(data, "info")


@pytest.mark.parametrize(
    "meter,occupied,status",
    [
        ("N", "V", "vacant"),
        ("N", "O", "occupied"),
        ("NU", "V", "not_for_use"),
        ("NU", "O", "not_for_use"),
        ("?", "V", "unknown"),
        ("N", "?", "unknown"),
    ],
)
def test_occupancy_is_reported_not_proven_current(meter, occupied, status):
    result = interpret_occupancy(occupancy(ParkingMeterStatus=meter, OccupancyStatus=occupied))
    assert result["reported_status"] == status
    assert result["current_status"] == result["observation_freshness"] == "unknown"
    assert result["occupancy_changed_at"] == "2021-01-20T13:00:00+08:00"
    assert interpret_occupancy(occupancy(OccupancyDateChanged="bad"))["occupancy_changed_at"] is None


def test_fixed_urls_bounded_client_and_invalid_operation():
    async def run():
        seen = []

        def respond(request):
            seen.append(str(request.url))
            data = (
                csv_bytes([space()])
                if str(request.url) == SPACE_INFO_URL
                else csv_bytes([occupancy()], "occupancy")
            )
            return httpx.Response(200, content=data)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = MeteredClient(http)
            assert (await client.fetch("info")).records == [space()]
            assert (await client.fetch("occupancy")).records == [occupancy()]
            with pytest.raises(ValueError):
                await client.fetch("https://example.test")
            assert seen == [SPACE_INFO_URL, OCCUPANCY_URL]
            with pytest.raises(UpstreamError, match="size limit"):
                await MeteredClient(http, max_bytes=10).fetch("info")
        for status in (302, 500):
            seen.clear()

            def reject(request, status=status):
                seen.append(request)
                return httpx.Response(status, headers={"location": "https://example.test"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
                with pytest.raises(UpstreamError):
                    await MeteredClient(http).fetch("info")
            assert len(seen) == 1
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("private upstream details"))
            )
        ) as http:
            with pytest.raises(UpstreamError, match="failed or timed out"):
                await MeteredClient(http).fetch("info")

    asyncio.run(run())


def test_search_pagination_cache_and_unknown_occupancy():
    async def run():
        gateway = Gateway()
        clock = [0]
        service = MeteredParkingService(gateway, monotonic=lambda: clock[0])
        first = await service.search_metered_spaces("測試道", "油尖旺", limit=1)
        assert first["total"] == 2 and first["items"][0]["ParkingSpaceId"] == "123A"
        second = await service.search_metered_spaces("測試道", "油尖旺", limit=1, cursor=first["next_cursor"])
        assert second["items"][0]["ParkingSpaceId"] == "123B" and not second["has_more"]
        assert gateway.calls == ["info"]
        for cursor in ("!", first["next_cursor"]):
            with pytest.raises(ValueError):
                await service.search_metered_spaces("other", cursor=cursor)
        result = await service.get_metered_occupancy(["123A", "123B", "124A", "999A"])
        assert [i["status"] for i in result["items"]] == ["ok", "ok", "missing_occupancy", "not_found"]
        assert result["items"][1]["record"]["reported_status"] == "not_for_use"
        await service.get_metered_occupancy(["123A"])
        assert gateway.calls == ["info", "occupancy", "occupancy"]
        await service.get_metered_occupancy(["999A"])
        assert gateway.calls == ["info", "occupancy", "occupancy"]
        gateway.rows.append(space("125A"))
        clock[0] = 3600
        with pytest.raises(ValueError, match="expired cursor"):
            await service.search_metered_spaces("測試道", "油尖旺", cursor=first["next_cursor"])
        gateway.fail = True
        clock[0] = 7200
        with pytest.raises(UpstreamError):
            await service.search_metered_spaces("測試道")

    asyncio.run(run())


def test_invalid_input_never_fetches():
    async def run():
        gateway = Gateway()
        service = MeteredParkingService(gateway)
        for ids in ([], ["123A"] * 2, ["a/b"], [True], ["123A"] * 11, "123A"):
            with pytest.raises(ValueError):
                await service.get_metered_occupancy(ids)
        for kwargs in (
            {"query": " "},
            {"query": "a", "limit": True},
            {"query": "a", "limit": 51},
            {"query": "a", "district": " "},
            {"query": "a", "cursor": ""},
        ):
            with pytest.raises(ValueError):
                await service.search_metered_spaces(**kwargs)
        assert not gateway.calls

    asyncio.run(run())


def test_metered_tool_registration_and_calls():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
            mcp = FastMCP("Synthetic metered test")
            register_tools(mcp, ParkingService(GovernmentClient(http)), MeteredParkingService(Gateway()))
            listing = await mcp.list_tools()
            assert {t.name for t in listing} == {
                "search_carparks",
                "get_carpark_vacancies",
                "search_metered_spaces",
                "get_metered_occupancy",
            }
            assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in listing)
            _, result = await mcp.call_tool("search_metered_spaces", {"query": "測試道"})
            assert result["total"] == 2
            _, result = await mcp.call_tool("get_metered_occupancy", {"parking_space_ids": ["123A"]})
            assert result["items"][0]["record"]["reported_status"] == "vacant"

    asyncio.run(run())
