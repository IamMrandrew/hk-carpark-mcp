"""Synthetic HTTP fixtures for boundary tests; never presented as real parking data."""

import asyncio
import importlib.util

import httpx


def test_fixed_government_endpoint_and_verified_response():
    assert importlib.util.find_spec("hkparking.client"), "Government client not implemented"
    from hkparking.client import GovernmentClient

    async def run():
        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(
                200, json={"results": [{"park_Id": "synthetic1", "name": "Synthetic Park"}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            gateway = GovernmentClient(http)
            result = await gateway.fetch("info", lang="zh_TW")
        assert len(seen) == 1
        assert seen[0].url.scheme == "https"
        assert seen[0].url.host == "api.data.gov.hk"
        assert seen[0].url.path == "/v1/carpark-info-vacancy"
        assert seen[0].url.params["data"] == "info"
        assert result.records == [{"park_Id": "synthetic1", "name": "Synthetic Park"}]
        assert result.fetched_at.tzinfo is not None
        assert result.source_url.startswith("https://api.data.gov.hk/v1/carpark-info-vacancy?")

    asyncio.run(run())


def test_upstream_failures_never_become_parking_data():
    import pytest

    from hkparking.client import GovernmentClient, UpstreamError

    async def run():
        cases = [
            httpx.Response(302, headers={"Location": "https://untrusted.example/"}),
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, json={"wrong": []}),
            httpx.Response(200, json={"results": [{"park_Id": "bad/id"}]}),
            httpx.Response(200, json={"results": [{"park_Id": "same"}, {"park_Id": "same"}]}),
        ]
        for response in cases:
            requests = []

            def reply(request, requests=requests, response=response):
                requests.append(request)
                return response

            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
                with pytest.raises(UpstreamError):
                    await GovernmentClient(http).fetch("info")
            assert len(requests) == 1, "Never follow upstream redirects"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 50))
        ) as http:
            with pytest.raises(UpstreamError, match="size limit"):
                await GovernmentClient(http, max_bytes=10).fetch("info")

    asyncio.run(run())


def test_invalid_upstream_parameters_do_not_make_network_requests():
    import pytest

    from hkparking.client import GovernmentClient

    async def run():
        seen = []

        def fail(request):
            seen.append(request)
            return httpx.Response(200, json={"results": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            gateway = GovernmentClient(http)
            for operation, kwargs in [
                ("arbitrary-url", {}),
                ("info", {"lang": "fr"}),
                ("vacancy", {"carpark_ids": ["https://evil.example"]}),
                ("vacancy", {"vehicle_type": "invalid"}),
            ]:
                with pytest.raises(ValueError):
                    await gateway.fetch(operation, **kwargs)
        assert seen == []

    asyncio.run(run())
