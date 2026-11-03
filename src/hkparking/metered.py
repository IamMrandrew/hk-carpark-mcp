"""Bounded official metered-space CSV queries; change time is not observation time."""

import asyncio
import base64
import csv
import io
import json
import re
import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from .client import UpstreamError, UpstreamResult
from .service import CatalogSnapshot, bounded_text, digest

SPACE_INFO_URL = "https://resource.data.one.gov.hk/td/psiparkingspaces/spaceinfo/parkingspaces.csv"
OCCUPANCY_URL = "https://resource.data.one.gov.hk/td/psiparkingspaces/occupancystatus/occupancystatus.csv"
SPACE_ID = re.compile(r"[0-9]{1,5}[A-Za-z]\Z")
INFO_FIELDS = (
    "PoleId",
    "ParkingSpaceId",
    "Region",
    "Region_tc",
    "Region_sc",
    "District",
    "District_tc",
    "District_sc",
    "SubDistrict",
    "SubDistrict_tc",
    "SubDistrict_sc",
    "Street",
    "Street_tc",
    "Street_sc",
    "SectionOfStreet",
    "SectionOfStreet_tc",
    "SectionOfStreet_sc",
    "Latitude",
    "Longitude",
    "VehicleType",
    "LPP",
    "OperatingPeriod",
    "TimeUnit",
    "PaymentUnit",
)
OCCUPANCY_FIELDS = ("ParkingSpaceId", "ParkingMeterStatus", "OccupancyStatus", "OccupancyDateChanged")


def parse_csv(content: bytes, kind: str) -> list[dict]:
    """Validate the official header/preamble and preserve source strings without inference."""
    fields = INFO_FIELDS if kind == "info" else OCCUPANCY_FIELDS
    try:
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), strict=True))
        if kind == "info" and rows and len(rows[0]) == 1:
            date.fromisoformat(rows.pop(0)[0])
            if rows and not any(rows[0]):
                rows.pop(0)
        if not rows or tuple(rows.pop(0)) != fields:
            raise ValueError()
        result, seen = [], set()
        for values in rows:
            if not values or not any(values):
                continue
            if len(values) != len(fields):
                raise ValueError()
            row = dict(zip(fields, values, strict=True))
            space_id = row["ParkingSpaceId"]
            if not SPACE_ID.fullmatch(space_id) or space_id in seen:
                raise ValueError()
            seen.add(space_id)
            if kind == "info":
                if not row["PoleId"].isdigit():
                    raise ValueError()
                if int(row["PoleId"]) > 90000:
                    continue  # Official specification identifies these as internal test meters.
                lat, lon = float(row["Latitude"]), float(row["Longitude"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError()
            result.append(row)
        return result
    except (ValueError, UnicodeError, csv.Error):
        raise UpstreamError("Official metered parking feed returned invalid CSV records") from None


class MeteredClient:
    def __init__(self, http: httpx.AsyncClient, timeout_seconds=10, max_bytes=8 * 1024 * 1024):
        if timeout_seconds <= 0 or max_bytes <= 0:
            raise ValueError("Positive upstream limits required")
        self.http, self.timeout_seconds, self.max_bytes = http, timeout_seconds, max_bytes

    async def fetch(self, kind: str) -> UpstreamResult:
        if kind not in ("info", "occupancy"):
            raise ValueError("Unsupported metered parking operation")
        url = SPACE_INFO_URL if kind == "info" else OCCUPANCY_URL
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self.http.stream(
                    "GET", url, follow_redirects=False, timeout=self.timeout_seconds
                ) as response:
                    if response.status_code != 200:
                        raise UpstreamError("Official metered parking feed unavailable (HTTP status error)")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise UpstreamError("Official metered parking feed exceeds size limit")
                        chunks.append(chunk)
            records = parse_csv(b"".join(chunks), kind)
        except (httpx.HTTPError, TimeoutError):
            raise UpstreamError("Official metered parking feed request failed or timed out") from None
        return UpstreamResult(records, url, datetime.now(UTC))


def interpret_occupancy(row: dict) -> dict:
    meter, occupancy = row["ParkingMeterStatus"], row["OccupancyStatus"]
    status = (
        "not_for_use"
        if meter == "NU"
        else ({"V": "vacant", "O": "occupied"}.get(occupancy, "unknown") if meter == "N" else "unknown")
    )
    changed = None
    try:
        changed = (
            datetime.strptime(row["OccupancyDateChanged"], "%m/%d/%Y %I:%M:%S %p")
            .replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
            .isoformat()
        )
    except ValueError:
        pass
    return {
        **row,
        "reported_status": status,
        "occupancy_changed_at": changed,
        "current_status": "unknown",
        "observation_freshness": "unknown",
    }


class MeteredParkingService:
    def __init__(self, gateway: MeteredClient, catalog_ttl_seconds=3600, monotonic=time.monotonic):
        if not 0 < catalog_ttl_seconds <= 3600:
            raise ValueError("Catalog cache TTL must be within 0..3600 seconds")
        self.gateway, self.catalog_ttl_seconds, self.monotonic = gateway, catalog_ttl_seconds, monotonic
        self._catalog = None
        self._lock = asyncio.Lock()

    async def catalog(self) -> CatalogSnapshot:
        async with self._lock:
            if self._catalog and self.monotonic() - self._catalog.cached_at < self.catalog_ttl_seconds:
                return self._catalog
            result = await self.gateway.fetch("info")
            items = tuple(sorted(result.records, key=lambda row: row["ParkingSpaceId"]))
            self._catalog = CatalogSnapshot(
                items, result.fetched_at, self.monotonic(), digest(items), (result.source_url,)
            )
            return self._catalog

    async def search_metered_spaces(self, query, district=None, limit=10, cursor=None) -> dict:
        query = bounded_text(query, "query", 120)
        district = bounded_text(district, "district", 80, optional=True)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 1024):
            raise ValueError("Invalid pagination cursor")
        snapshot = await self.catalog()
        matched = []
        for row in snapshot.items:
            text = " ".join(row[k] for k in INFO_FIELDS[2:17]).casefold()
            district_text = " ".join(row[k] for k in ("District", "District_tc", "District_sc")).casefold()
            if all(term in text for term in query.split()) and (
                district is None or all(term in district_text for term in district.split())
            ):
                matched.append(row)
        binding, offset = digest(["metered", query, district]), 0
        if cursor:
            try:
                payload = json.loads(
                    base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(payload, dict)
                    or payload.get("v") != 1
                    or payload.get("q") != binding
                    or payload.get("snapshot") != snapshot.fingerprint
                    or type(payload.get("offset")) is not int
                    or not 0 <= payload["offset"] < len(matched)
                ):
                    raise ValueError()
                offset = payload["offset"]
            except (ValueError, TypeError, UnicodeError):
                raise ValueError("Invalid, mismatched or expired cursor; restart the search") from None
        items = matched[offset : offset + limit]
        next_offset = offset + len(items)
        more = next_offset < len(matched)
        payload = {"v": 1, "q": binding, "snapshot": snapshot.fingerprint, "offset": next_offset}
        return {
            "items": items,
            "total": len(matched),
            "returned": len(items),
            "has_more": more,
            "next_cursor": base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            if more
            else None,
            "catalog_fetched_at": snapshot.fetched_at.isoformat(),
            "source_urls": list(snapshot.sources),
            "disclaimer": "Location and tariff data only, not availability. Some meters may not yet be commissioned.",
        }

    async def get_metered_occupancy(self, parking_space_ids) -> dict:
        if (
            not isinstance(parking_space_ids, list)
            or not 1 <= len(parking_space_ids) <= 10
            or any(not isinstance(i, str) or not SPACE_ID.fullmatch(i) for i in parking_space_ids)
            or len(set(parking_space_ids)) != len(parking_space_ids)
        ):
            raise ValueError("Provide 1..10 unique, literal parking space IDs from search_metered_spaces")
        snapshot = await self.catalog()
        known = {row["ParkingSpaceId"]: row for row in snapshot.items}
        result = await self.gateway.fetch("occupancy") if any(i in known for i in parking_space_ids) else None
        occupancy = {row["ParkingSpaceId"]: row for row in result.records} if result else {}
        items = []
        for space_id in parking_space_ids:
            if space_id not in known:
                items.append({"ParkingSpaceId": space_id, "status": "not_found", "record": None})
            else:
                row = occupancy.get(space_id)
                items.append(
                    {
                        **known[space_id],
                        "status": "ok" if row else "missing_occupancy",
                        "record": interpret_occupancy(row) if row else None,
                    }
                )
        return {
            "items": items,
            "fetched_at": (result.fetched_at if result else datetime.now(UTC)).isoformat(),
            "source_url": result.source_url if result else None,
            "catalog_fetched_at": snapshot.fetched_at.isoformat(),
            "freshness_policy": "OccupancyDateChanged is the last state change, not a sensor observation time. "
            "A successful fetch does not establish sensor freshness; current status stays unknown.",
            "disclaimer": "Reported occupancy is not guaranteed availability or permission to park. Check signs on arrival.",
        }
