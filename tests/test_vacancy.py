"""Synthetic vacancy fixtures only; these are not live government observations."""

import importlib.util
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

FETCHED_AT = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)


def interpret(raw, **kwargs):
    """Load the real interpreter, including a clear failure before it exists."""
    assert importlib.util.find_spec("hkparking.vacancy") is not None, "interpreter module missing"
    from hkparking.vacancy import interpret_record

    return interpret_record(raw, FETCHED_AT, **kwargs)


@pytest.mark.parametrize("count", [0, 7, 10**30])
def test_synthetic_type_a_preserves_exact_count_without_timestamp(count):
    raw = {"category": "MONTHLY", "vacancy_type": "A", "vacancy": count}
    assert interpret(raw) == {
        "category": "MONTHLY",
        "vacancy_type": "A",
        "vacancy": count,
        "lastupdate": None,
        "lastupdate_iso": None,
        "freshness": "unknown",
        "age_seconds": None,
        "reported_status": "exact_count",
        "reported_count": count,
        "current_status": "unknown",
        "current_count": None,
    }
    assert raw == {"category": "MONTHLY", "vacancy_type": "A", "vacancy": count}


@pytest.mark.parametrize("value,status", [(1, "available"), (0, "full")])
def test_synthetic_type_b_reports_availability_not_count(value, status):
    result = interpret({"vacancy_type": "B", "vacancy": value})
    assert result["reported_status"] == status
    assert result["reported_count"] is None
    assert result["category"] is None


@pytest.mark.parametrize("vacancy_type", ["A", "B"])
def test_synthetic_minus_one_is_missing_data(vacancy_type):
    result = interpret({"vacancy_type": vacancy_type, "vacancy": -1})
    assert result["reported_status"] == "missing_data"
    assert result["reported_count"] is None


def test_synthetic_type_c_zero_is_closed_without_count():
    result = interpret({"vacancy_type": "C", "vacancy": 0})
    assert result["reported_status"] == "closed"
    assert result["reported_count"] is None


@pytest.mark.parametrize(
    "vacancy_type,value",
    [(t, v) for t in ("A", "B", "C") for v in (True, False, 0.0, 1.0, -1.0, "0", None, [], {})]
    + [("A", -2), ("B", -2), ("B", 2), ("C", -1), ("C", 1)],
)
def test_synthetic_invalid_type_value_pairs_do_not_decode(vacancy_type, value):
    result = interpret({"vacancy_type": vacancy_type, "vacancy": value})
    assert result["reported_status"] == "invalid_value"
    assert result["reported_count"] is None
    assert result["vacancy"] == value


@pytest.mark.parametrize("vacancy_type", [None, "", "D", "a", " A ", True, 1, [], {}])
@pytest.mark.parametrize("value", [0, None, True])
def test_synthetic_unknown_type_never_decodes(vacancy_type, value):
    result = interpret({"vacancy_type": vacancy_type, "vacancy": value})
    assert result["reported_status"] == "unknown_type"
    assert result["reported_count"] is None
    assert result["vacancy_type"] == vacancy_type


def test_synthetic_missing_raw_fields_are_not_fabricated():
    result = interpret({})
    assert result["reported_status"] == "unknown_type"
    for field in ("category", "vacancy_type", "vacancy", "lastupdate", "reported_count"):
        assert result[field] is None


@pytest.mark.parametrize(
    "vacancy_type,value,status,count",
    [
        ("A", 7, "exact_count", 7),
        ("A", 0, "exact_count", 0),
        ("B", 1, "available", None),
        ("B", 0, "full", None),
        ("C", 0, "closed", None),
        ("A", -1, "missing_data", None),
        ("B", -1, "missing_data", None),
        ("C", 1, "invalid_value", None),
        ("D", 0, "unknown_type", None),
    ],
)
def test_synthetic_naive_hong_kong_timestamp_exposes_fresh_decoded_values(vacancy_type, value, status, count):
    raw = {
        "category": "HOURLY",
        "vacancy_type": vacancy_type,
        "vacancy": value,
        "lastupdate": "2026-01-01 12:00:00",
    }
    result = interpret(raw)
    assert result["lastupdate"] == raw["lastupdate"]
    assert result["lastupdate_iso"] == "2026-01-01T12:00:00+08:00"
    assert result["age_seconds"] == 0
    assert result["freshness"] == "fresh"
    assert result["current_status"] == status
    assert result["current_count"] == count
    assert result["reported_status"] == status
    assert result["reported_count"] == count


@pytest.mark.parametrize(
    "age,freshness",
    [
        (-120.000001, "future_timestamp"),
        (-120, "fresh"),
        (-1, "fresh"),
        (0, "fresh"),
        (299.999999, "fresh"),
        (300, "fresh"),
        (300.000001, "stale"),
        (86400, "stale"),
    ],
)
@pytest.mark.parametrize(
    "vacancy_type,value,status,count",
    [("A", 7, "exact_count", 7), ("B", 1, "available", None), ("C", 0, "closed", None)],
)
def test_synthetic_signed_freshness_boundaries_preserve_history(
    age, freshness, vacancy_type, value, status, count
):
    timestamp = (FETCHED_AT - timedelta(seconds=age)).isoformat()
    result = interpret({"vacancy_type": vacancy_type, "vacancy": value, "lastupdate": timestamp})
    assert result["age_seconds"] == pytest.approx(age)
    assert result["lastupdate_iso"] == timestamp
    assert result["freshness"] == freshness
    assert result["reported_status"] == status
    assert result["reported_count"] == count
    assert result["current_status"] == (status if freshness == "fresh" else "unknown")
    assert result["current_count"] == (count if freshness == "fresh" else None)


@pytest.mark.parametrize(
    "age,freshness", [(-11, "future_timestamp"), (-10, "fresh"), (20, "fresh"), (21, "stale")]
)
def test_synthetic_custom_freshness_policy(age, freshness):
    timestamp = (FETCHED_AT - timedelta(seconds=age)).isoformat()
    result = interpret(
        {"vacancy_type": "A", "vacancy": 7, "lastupdate": timestamp},
        max_age_seconds=20,
        future_tolerance_seconds=10,
    )
    assert result["freshness"] == freshness


@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        "",
        "not-a-timestamp",
        "2026-02-30T12:00:00",
        "2026-01-01",
        "20260101",
        "12:00:00",
        0,
        True,
        [],
        {},
        datetime(2026, 1, 1, 12, tzinfo=UTC).replace(tzinfo=None),
        "2026-01-01T12:00:00+25:00",
    ],
)
@pytest.mark.parametrize(
    "vacancy_type,value,status,count",
    [("A", 7, "exact_count", 7), ("B", 1, "available", None), ("C", 0, "closed", None)],
)
def test_synthetic_invalid_timestamps_suppress_current_values(timestamp, vacancy_type, value, status, count):
    result = interpret({"vacancy_type": vacancy_type, "vacancy": value, "lastupdate": timestamp})
    assert result["lastupdate"] == timestamp
    assert result["lastupdate_iso"] is None
    assert result["age_seconds"] is None
    assert result["freshness"] == "unknown"
    assert result["reported_status"] == status
    assert result["reported_count"] == count
    assert result["current_status"] == "unknown"
    assert result["current_count"] is None


@pytest.mark.parametrize(
    "fetched_at", [datetime(2026, 1, 1, 4, tzinfo=UTC).replace(tzinfo=None), None, "2026-01-01T04:00:00Z", 0]
)
def test_synthetic_invalid_fetch_context_is_rejected_even_without_timestamp(fetched_at):
    from hkparking.vacancy import interpret_record

    with pytest.raises(ValueError, match="fetched_at"):
        interpret_record({}, fetched_at)


@pytest.mark.parametrize("field", ["max_age_seconds", "future_tolerance_seconds"])
@pytest.mark.parametrize(
    "value", [-1, -0.1, True, False, None, "300", float("nan"), float("inf"), float("-inf"), [], {}]
)
def test_synthetic_invalid_policy_thresholds_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        interpret({}, **{field: value})


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01T12:00:00+08:99",
        "2026-01-01T12:00:00-00:60",
        "2026-01-01X12:00:00",
        "2026-01-01T12:00:00+08",
    ],
)
def test_synthetic_malformed_iso_timestamp_is_not_normalized(timestamp):
    result = interpret({"vacancy_type": "A", "vacancy": 7, "lastupdate": timestamp})
    assert result["freshness"] == "unknown"
    assert result["lastupdate_iso"] is None
    assert result["age_seconds"] is None
    assert result["current_count"] is None


@pytest.mark.parametrize(
    "timestamp,expected_iso",
    [
        ("2026-01-01T04:00:00Z", "2026-01-01T04:00:00+00:00"),
        ("2026-01-01T09:30:00+05:30", "2026-01-01T09:30:00+05:30"),
        ("2025-12-31T23:00:00-05:00", "2025-12-31T23:00:00-05:00"),
    ],
)
def test_synthetic_explicit_offsets_preserve_instant(timestamp, expected_iso):
    from hkparking.vacancy import interpret_record

    fetched_at = FETCHED_AT.astimezone(timezone(timedelta(hours=8)))
    result = interpret_record({"vacancy_type": "A", "vacancy": 7, "lastupdate": timestamp}, fetched_at)
    assert result["lastupdate_iso"] == expected_iso
    assert result["lastupdate"] == timestamp
    assert result["age_seconds"] == 0
    assert result["freshness"] == "fresh"
    assert result["current_count"] == 7


@pytest.mark.parametrize(
    "age,freshness", [(-0.5, "fresh"), (-0.500001, "future_timestamp"), (0.5, "fresh"), (0.500001, "stale")]
)
def test_synthetic_fractional_policy_seconds_are_supported(age, freshness):
    timestamp = (FETCHED_AT - timedelta(seconds=age)).isoformat()
    result = interpret(
        {"vacancy_type": "A", "vacancy": 7, "lastupdate": timestamp},
        max_age_seconds=0.5,
        future_tolerance_seconds=0.5,
    )
    assert result["freshness"] == freshness


def test_synthetic_zero_policy_only_accepts_same_instant():
    raw = {"vacancy_type": "A", "vacancy": 7, "lastupdate": FETCHED_AT.isoformat()}
    result = interpret(raw, max_age_seconds=0, future_tolerance_seconds=0)
    assert result["freshness"] == "fresh"


def test_synthetic_historical_hong_kong_fold_uses_elapsed_time_not_wall_clock():
    from hkparking.vacancy import interpret_record

    fetched_at = datetime(1979, 10, 21, 2, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"), fold=1)
    result = interpret_record(
        {"vacancy_type": "A", "vacancy": 7, "lastupdate": "1979-10-21 02:30:00"}, fetched_at
    )
    assert result["age_seconds"] == 3600
    assert result["freshness"] == "stale"
    assert result["current_count"] is None
