"""Synthetic fixtures for catalog ID validation and vehicle/category preservation."""

import asyncio
from datetime import UTC, datetime

import httpx

from hkparking.client import GovernmentClient
from hkparking.service import ParkingService


def test_unknown_ids_do_not_fetch_or_invent_vacancy():
    async def run():
        seen = []

        def reply(request):
            seen.append(request.url.params["data"])
            return httpx.Response(
                200, json={"results": [{"park_Id": "synthetic1", "name": "Synthetic Park"}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            service = ParkingService(GovernmentClient(http))
            result = await service.get_carpark_vacancies(["UnknownID"])
            assert result["items"] == [
                {"park_Id": "UnknownID", "status": "not_found", "vehicle_type": "privateCar", "records": []}
            ]
            assert result["source_url"] is None
            assert seen == ["info", "info"]

    asyncio.run(run())


def test_known_ids_fetch_fresh_preserve_categories_and_ignore_other_vehicles():
    async def run():
        seen = []
        timestamp = datetime.now(UTC).isoformat()

        def reply(request):
            seen.append(dict(request.url.params))
            if request.url.params["data"] == "info":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"park_Id": f"synthetic{i}", "name": f"Synthetic Park {i}"} for i in [1, 2, 3]
                        ]
                    },
                )
            assert request.url.params["carparkIds"] == "synthetic1,synthetic2,synthetic3"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "park_Id": "synthetic1",
                            "privateCar": [
                                {
                                    "category": "HOURLY",
                                    "vacancy_type": "A",
                                    "vacancy": 7,
                                    "lastupdate": timestamp,
                                },
                                {
                                    "category": "DAILY",
                                    "vacancy_type": "B",
                                    "vacancy": 1,
                                    "lastupdate": timestamp,
                                },
                                {
                                    "category": "MONTHLY",
                                    "vacancy_type": "A",
                                    "vacancy": -1,
                                    "lastupdate": timestamp,
                                },
                            ],
                            "motorCycle": [
                                {
                                    "category": "HOURLY",
                                    "vacancy_type": "A",
                                    "vacancy": 999,
                                    "lastupdate": timestamp,
                                }
                            ],
                        },
                        {"park_Id": "synthetic3", "motorCycle": []},
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            service = ParkingService(GovernmentClient(http))
            args = ["UnknownID", "synthetic1", "synthetic2", "synthetic3"]
            result = await service.get_carpark_vacancies(args)
            assert [item["park_Id"] for item in result["items"]] == args
            assert [item["status"] for item in result["items"]] == [
                "not_found",
                "ok",
                "missing_vacancy",
                "missing_vehicle_records",
            ]
            records = result["items"][1]["records"]
            assert [r["category"] for r in records] == ["HOURLY", "DAILY", "MONTHLY"]
            assert [r["current_count"] for r in records] == [7, None, None]
            assert [r["current_status"] for r in records] == ["exact_count", "available", "missing_data"]
            assert result["freshness_policy"]["max_age_seconds"] == 300
            assert result["source_url"].startswith("https://api.data.gov.hk/v1/carpark-info-vacancy?")
            assert result["items"][1]["default_category"] == "HOURLY"
            await service.get_carpark_vacancies(args)
            assert len([r for r in seen if r["data"] == "vacancy"]) == 2, "No vacancy caching"

    asyncio.run(run())


def test_invalid_vacancy_inputs_fail_before_any_upstream_request():
    import pytest

    async def run():
        seen = []

        def reply(request):
            seen.append(request)
            return httpx.Response(200, json={"results": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            service = ParkingService(GovernmentClient(http))
            for ids, vehicle in [
                ([], "privateCar"),
                (["same", "same"], "privateCar"),
                (["id"] * 11, "privateCar"),
                ([" tdcp3"], "privateCar"),
                (["http://host"], "privateCar"),
                ([True], "privateCar"),
                (["id"], "PrivateCar"),
                ("tdcp3", "privateCar"),
            ]:
                with pytest.raises(ValueError):
                    await service.get_carpark_vacancies(ids, vehicle)
            assert seen == []

    asyncio.run(run())
