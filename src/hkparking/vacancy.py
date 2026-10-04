"""Deterministic interpretation of individual official vacancy records."""

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?"
)


def interpret_record(
    raw: dict,
    fetched_at: datetime,
    max_age_seconds=300,
    future_tolerance_seconds=120,
) -> dict:
    """Preserve reported facts separately from usable current availability.

    Naive upstream timestamps explicitly mean Asia/Hong_Kong local time.
    Accept extended ISO date/time with T or space, optional seconds and up to
    six fractional digits, and optional Z or +/-HH:MM offset. Date-only and
    malformed timestamps fail closed rather than being normalized.
    Fetch context must be timezone-aware; policy seconds must be finite,
    nonnegative int/float values (not booleans). Freshness is not an arrival
    availability guarantee. Missing raw fields remain None, never zero.
    """
    fetched_offset = fetched_at.utcoffset() if isinstance(fetched_at, datetime) else None
    if fetched_offset is None:
        raise ValueError("fetched_at must be a timezone-aware datetime")
    for name, value in (
        ("max_age_seconds", max_age_seconds),
        ("future_tolerance_seconds", future_tolerance_seconds),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise ValueError(f"{name} must be finite nonnegative seconds")
    vacancy_type = raw.get("vacancy_type")
    vacancy = raw.get("vacancy")
    reported_status = "invalid_value"
    reported_count = None
    if vacancy_type not in ("A", "B", "C"):
        reported_status = "unknown_type"
    elif not isinstance(vacancy, int) or isinstance(vacancy, bool):
        pass
    elif vacancy_type in ("A", "B") and vacancy == -1:
        reported_status = "missing_data"
    elif vacancy_type == "A" and vacancy >= 0:
        reported_status = "exact_count"
        reported_count = vacancy
    elif vacancy_type == "C" and vacancy == 0:
        reported_status = "closed"
    elif vacancy_type == "B" and vacancy in (0, 1):
        reported_status = "available" if vacancy == 1 else "full"
    lastupdate_iso = None
    age_seconds = None
    freshness = "unknown"
    timestamp = raw.get("lastupdate")
    if isinstance(timestamp, str) and _TIMESTAMP.fullmatch(timestamp):
        try:
            updated_at = datetime.fromisoformat(timestamp)
        except ValueError:
            pass
        else:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
            lastupdate_iso = updated_at.isoformat()
            # Same-zone datetime subtraction ignores offset changes/folds.
            # Subtract offsets explicitly, without overflowing datetime at its bounds.
            wall_delta = fetched_at.replace(tzinfo=None) - updated_at.replace(tzinfo=None)
            updated_offset = updated_at.utcoffset()
            assert updated_offset is not None  # Fixed-offset ISO or Asia/Hong_Kong above.
            offset_delta = fetched_offset - updated_offset
            age_seconds = (wall_delta - offset_delta).total_seconds()
            if age_seconds < -future_tolerance_seconds:
                freshness = "future_timestamp"
            elif age_seconds > max_age_seconds:
                freshness = "stale"
            else:
                freshness = "fresh"
    return {
        "category": raw.get("category"),
        "vacancy_type": raw.get("vacancy_type"),
        "vacancy": raw.get("vacancy"),
        "lastupdate": raw.get("lastupdate"),
        "lastupdate_iso": lastupdate_iso,
        "freshness": freshness,
        "age_seconds": age_seconds,
        "reported_status": reported_status,
        "reported_count": reported_count,
        "current_status": reported_status if freshness == "fresh" else "unknown",
        "current_count": reported_count if freshness == "fresh" else None,
    }
