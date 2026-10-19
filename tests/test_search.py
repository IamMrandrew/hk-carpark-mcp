"""Synthetic bilingual government responses for deterministic search tests."""

import asyncio
import importlib.util

import httpx

CATALOG = {
    "zh_TW": [
        {
            "park_Id": "synthetic1",
            "name": "荃灣合成停車場",
            "displayAddress": "合成青山公路",
            "district": "荃灣區",
            "opening_status": "OPEN",
        },
        {"park_Id": "synthetic2", "name": "荃灣合成廣場", "displayAddress": "合成街", "district": "荃灣區"},
    ],
    "en_US": [
        {
            "park_Id": "synthetic1",
            "name": "Synthetic Tsuen Wan Car Park",
            "displayAddress": "Synthetic Castle Peak Road",
            "district": "Tsuen Wan",
        },
        {
            "park_Id": "synthetic2",
            "name": "Synthetic Tsuen Wan Plaza",
            "displayAddress": "Synthetic Street",
            "district": "Tsuen Wan",
        },
    ],
}


def test_bilingual_search_and_exact_pagination():
    assert importlib.util.find_spec("hkparking.service"), "Parking search service not implemented"
    from hkparking.client import GovernmentClient
    from hkparking.service import ParkingService

    async def run():
        requests = []

        def reply(request):
            requests.append(request)
            return httpx.Response(200, json={"results": CATALOG[request.url.params["lang"]]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            service = ParkingService(GovernmentClient(http))
            first = await service.search_carparks("荃灣", limit=1)
            assert first["total"] == 2 and first["returned"] == 1 and first["has_more"]
            assert first["items"][0]["park_Id"] == "synthetic1"
            assert first["items"][0]["name_en"] == "Synthetic Tsuen Wan Car Park"
            second = await service.search_carparks("荃灣", limit=1, cursor=first["next_cursor"])
            assert second["items"][0]["park_Id"] == "synthetic2"
            assert second["total"] == 2 and second["returned"] == 1 and not second["has_more"]
            assert second["next_cursor"] is None
            english = await service.search_carparks("castle peak", district="Tsuen Wan")
            assert [r["park_Id"] for r in english["items"]] == ["synthetic1"]
            colloquial = await service.search_carparks("荃灣多層停車場")
            assert colloquial["total"] == 0 and colloquial["items"] == []
            assert len(requests) == 2, "Catalog cache should avoid repeated upstream requests"

    asyncio.run(run())


def test_search_rejects_unbounded_inputs_and_mismatched_cursor():
    import pytest

    from hkparking.client import GovernmentClient
    from hkparking.service import ParkingService

    async def run():
        seen = []

        def reply(request):
            seen.append(request)
            return httpx.Response(200, json={"results": CATALOG[request.url.params["lang"]]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            service = ParkingService(GovernmentClient(http))
            for kwargs in [
                {"query": " "},
                {"query": "x" * 121},
                {"query": "荃灣", "limit": 0},
                {"query": "荃灣", "limit": True},
                {"query": "荃灣", "district": " "},
                {"query": "荃灣", "cursor": "x" * 1025},
            ]:
                with pytest.raises(ValueError):
                    await service.search_carparks(**kwargs)
            assert seen == []
            page = await service.search_carparks("荃灣", limit=1)
            with pytest.raises(ValueError, match="cursor"):
                await service.search_carparks("合成", cursor=page["next_cursor"])
            with pytest.raises(ValueError, match="cursor"):
                await service.search_carparks("荃灣", cursor="not-a-json-cursor")

    asyncio.run(run())


def test_expired_catalog_cache_has_no_stale_fallback_on_api_failure():
    import pytest

    from hkparking.client import GovernmentClient, UpstreamError
    from hkparking.service import ParkingService

    async def run():
        clock = [0.0]
        failed = [False]

        def reply(request):
            return (
                httpx.Response(503)
                if failed[0]
                else httpx.Response(200, json={"results": CATALOG[request.url.params["lang"]]})
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            service = ParkingService(
                GovernmentClient(http), catalog_ttl_seconds=1, monotonic=lambda: clock[0]
            )
            assert (await service.search_carparks("荃灣"))["total"] == 2
            clock[0] = 2
            failed[0] = True
            with pytest.raises(UpstreamError):
                await service.search_carparks("荃灣")

    asyncio.run(run())
