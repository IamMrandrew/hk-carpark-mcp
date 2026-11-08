import asyncio
from datetime import UTC, datetime

import pytest

from hkparking.client import UpstreamResult
from hkparking.hkemobility import HKeMobilityService
from hkparking.hkemobility_source import (
    ProbeError,
    freshness,
    normalize_mini,
    normalize_wfs,
    signing_key_from_bundle,
    vacancy_state,
    valid_id,
    validate_wfs,
)

NOW = datetime(2026, 11, 8, 1, tzinfo=UTC)


def feature(**values):
    return {"type": "Feature", "properties": {
        "EPARK_ID": "tdc106p3", "PARK_ID": "911202", "VACANCY_TYPE": "A",
        "VACANCY": -1, "OPENING_STATUS": "O", "LAST_UPDATE": NOW.isoformat(), **values,
    }}


@pytest.mark.parametrize("value", ["' OR 1=1", "http://host/", "", None, "a" * 65])
def test_reject_ids(value):
    with pytest.raises(ProbeError):
        valid_id(value)


@pytest.mark.parametrize("value,status", [(None, "missing"), ("x", "invalid"),
    ("2026-11-08 01:00:00", "unknown_timezone"), ("2026-11-09T01:00:00Z", "future")])
def test_uncertain_time(value, status):
    assert freshness(value, NOW, 15)["timestamp_status"] == status


def test_unknown_and_stale_not_zero():
    assert normalize_wfs(feature(), NOW, 15)["current_vacancy_count"] is None
    stale = normalize_wfs(feature(VACANCY=30, OPENING_STATUS="OPEN", LAST_UPDATE="2026-10-14T00:00:00Z"), NOW, 15)
    assert stale["raw_vacancy"] == 30
    assert stale["current_vacancy_count"] is None
    assert normalize_wfs(feature(VACANCY=99), NOW, 15)["flags"] == ["unrecognized_opening_status"]
    assert vacancy_state("A", -1) == "UNKNOWN"
    assert vacancy_state("B", 1) == "AVAILABLE"
    assert vacancy_state("C", 99) == "CLOSED"


def test_capacity_not_vacancy():
    data = {"park_id": "911202", "epark_id": "TDC106P3", "basic_info": {"isClosed": False},
            "parking_space": [{"car_type": "P", "hourly": {
                "space": {"val": 55}, "vacancy": {"mode": "A", "val": -1}}}]}
    row = normalize_mini(data, NOW, 15)[0]
    assert row["raw_capacity"] == 55
    assert row["current_vacancy_count"] is None


@pytest.mark.parametrize("features,extras", [([feature(), feature(PARK_ID="other")], {}),
    ([feature(EPARK_ID="wrong")], {}), ([feature()], {"numberMatched": 2})])
def test_reject_incomplete_or_ambiguous_wfs(features, extras):
    with pytest.raises(ProbeError):
        validate_wfs({"type": "FeatureCollection", "features": features, **extras}, ["tdc106p3"])


def test_changed_contract_fails_closed():
    with pytest.raises(ProbeError):
        signing_key_from_bundle("new contract")


def test_official_conflict_and_broadened_ids(monkeypatch):
    row = {"epark_id": "TDC327P1", "current_vacancy_count": 99, "flags": [], "vacancy_state": "COUNT"}
    monkeypatch.setattr("hkparking.hkemobility.run", lambda _: {"normalized": {"wfs": [row]}})

    class Government:
        async def fetch(self, *args, **kwargs):
            return UpstreamResult([{"park_Id": "tdc327p1", "opening_status": "CLOSED"},
                                   {"park_Id": "unrequested"}], "https://api.data.gov.hk/", NOW)

    result = asyncio.run(HKeMobilityService(Government()).inspect(["tdc327p1"]))
    assert len(result["official_info"]["records"]) == 1
    assert row["current_vacancy_count"] is None
    assert "cross_source_opening_conflict" in row["flags"]


def test_reverse_identity_rejected():
    with pytest.raises(ProbeError):
        validate_wfs({"type": "FeatureCollection", "features": [feature(), feature(EPARK_ID="another")]},
                     ["tdc106p3", "another"])


def test_opening_disagreement_suppresses_both():
    from hkparking.hkemobility_source import compare_rows
    w = normalize_wfs(feature(VACANCY=99, OPENING_STATUS="OPEN", VEHICLE_TYPE="P", CATEGORY="HOURLY"), NOW, 15)
    m = dict(w, raw_opening_status=True)
    assert compare_rows([w], [m])[0]["reason"] == "source_opening_states_differ"
    assert w["current_vacancy_count"] is None


def test_cancelled_call_keeps_worker_serialized(monkeypatch):
    import threading
    release = threading.Event()
    started = threading.Event()

    def blocking(_):
        started.set()
        release.wait(3)
        return {"normalized": {}}

    monkeypatch.setattr("hkparking.hkemobility.run", blocking)

    async def scenario():
        service = HKeMobilityService(None)
        task = asyncio.create_task(service.inspect(["tdc106p3"]))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service._lock.locked()
        release.set()
        for _ in range(100):
            if not service._lock.locked():
                break
            await asyncio.sleep(0.01)
        assert not service._lock.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 403, 500])
def test_source_does_not_follow_or_retry(monkeypatch, status):
    import httpx

    from hkparking import hkemobility_source as source
    calls = []
    original = httpx.Client

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://untrusted.example/"})

    monkeypatch.setattr(source.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ProbeError):
        source.Client(10, "EN").request("GET", source.ORIGIN + "/miniapp_mobile/")
    assert len(calls) == 1


def test_stream_elapsed_budget(monkeypatch):
    import httpx

    from hkparking import hkemobility_source as source
    original = httpx.Client
    monkeypatch.setattr(source.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"hello")), **kw))
    times = iter([0, 11])
    monkeypatch.setattr(source.time, "monotonic", lambda: next(times))
    with pytest.raises(ProbeError, match="elapsed-time"):
        source.Client(10, "EN").request("GET", source.ORIGIN + "/miniapp_mobile/")


def test_internal_detail_route_accepts_missing_epark(monkeypatch):
    from hkparking import hkemobility_source as source
    monkeypatch.setattr(source.Client, "load_contract", lambda _: {})
    monkeypatch.setattr(source.Client, "detail", lambda _, park: ({
        "park_id": park, "basic_info": {"isClosed": False},
        "parking_space": [{"car_type": "P", "hourly": {
            "space": {"val": 100}, "vacancy": {"mode": "A", "val": -1}}}],
    }, {"status": 200}))
    result = source.read_park_details(["10015"])
    row = result["normalized"]["miniapp"][0]
    assert row["park_id"] == "10015"
    assert row["epark_id"] is None
    assert row["raw_capacity"] == 100
    assert row["current_vacancy_count"] is None


def test_internal_detail_route_rejects_wrong_identity(monkeypatch):
    from hkparking import hkemobility_source as source
    monkeypatch.setattr(source.Client, "load_contract", lambda _: {})
    monkeypatch.setattr(source.Client, "detail", lambda _, park: ({"park_id": "wrong"}, {}))
    with pytest.raises(ProbeError, match="different park ID"):
        source.read_park_details(["10015"])
