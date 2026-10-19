"""Thin bounded parking queries. Keyword filtering only; Grok chooses candidates."""

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .client import ID_PATTERN, VEHICLE_TYPES, GovernmentClient, UpstreamError


@dataclass(frozen=True)
class CatalogSnapshot:
    items: tuple[dict, ...]
    fetched_at: datetime
    cached_at: float
    fingerprint: str
    sources: tuple[str, ...]


def bounded_text(value, name, maximum, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-blank string of at most {maximum} characters")
    return " ".join(value.casefold().split())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ParkingService:
    def __init__(self, gateway: GovernmentClient, catalog_ttl_seconds=3600, monotonic=time.monotonic):
        if not 0 < catalog_ttl_seconds <= 3600:
            raise ValueError("Catalog cache TTL must be within 0..3600 seconds")
        self.gateway = gateway
        self.catalog_ttl_seconds = catalog_ttl_seconds
        self.monotonic = monotonic
        self._catalog = None
        self._lock = asyncio.Lock()

    async def catalog(self) -> CatalogSnapshot:
        async with self._lock:
            if self._catalog and self.monotonic() - self._catalog.cached_at < self.catalog_ttl_seconds:
                return self._catalog
            zh, en = await asyncio.gather(
                self.gateway.fetch("info", lang="zh_TW"), self.gateway.fetch("info", lang="en_US")
            )
            merged = {}
            for lang, result in [("zh", zh), ("en", en)]:
                for row in result.records:
                    item = merged.setdefault(
                        row["park_Id"],
                        {
                            "park_Id": row["park_Id"],
                            "name_zh": None,
                            "name_en": None,
                            "address_zh": None,
                            "address_en": None,
                            "district_zh": None,
                            "district_en": None,
                            "opening_status": None,
                        },
                    )
                    for incoming, outgoing in [
                        ("name", "name"),
                        ("displayAddress", "address"),
                        ("district", "district"),
                    ]:
                        value = row.get(incoming)
                        if value is not None and not isinstance(value, str):
                            raise UpstreamError("Official parking catalog returned invalid text fields")
                        item[outgoing + "_" + lang] = value
                    if lang == "zh" or item["opening_status"] is None:
                        item["opening_status"] = row.get("opening_status")
            items = tuple(merged[k] for k in sorted(merged))
            snapshot = CatalogSnapshot(
                items,
                min(zh.fetched_at, en.fetched_at),
                self.monotonic(),
                digest(items),
                (zh.source_url, en.source_url),
            )
            self._catalog = snapshot
            return snapshot

    async def search_carparks(
        self, query: str, district: str | None = None, limit: int = 10, cursor: str | None = None
    ) -> dict:
        query = bounded_text(query, "query", 120)
        district = bounded_text(district, "district", 80, optional=True)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 1024):
            raise ValueError("Invalid pagination cursor")
        snapshot = await self.catalog()
        binding = digest([query, district])
        terms = query.split()
        matched = []
        for item in snapshot.items:
            text = " ".join(
                item[k] or ""
                for k in ["name_zh", "name_en", "address_zh", "address_en", "district_zh", "district_en"]
            ).casefold()
            district_text = " ".join(item[k] or "" for k in ["district_zh", "district_en"]).casefold()
            if all(term in text for term in terms) and (
                district is None or all(term in district_text for term in district.split())
            ):
                matched.append(item)
        offset = 0
        if cursor:
            try:
                encoded = cursor + "=" * (-len(cursor) % 4)
                payload = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
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
            except (ValueError, TypeError, UnicodeDecodeError):
                raise ValueError("Invalid, mismatched or expired cursor; restart the search") from None
        selected = matched[offset : offset + limit]
        next_offset = offset + len(selected)
        has_more = next_offset < len(matched)
        next_cursor = None
        if has_more:
            payload = {"v": 1, "q": binding, "snapshot": snapshot.fingerprint, "offset": next_offset}
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return {
            "items": selected,
            "total": len(matched),
            "returned": len(selected),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "catalog_fetched_at": snapshot.fetched_at.isoformat(),
            "source_urls": list(snapshot.sources),
        }

    async def get_carpark_vacancies(self, carpark_ids: list[str], vehicle_type: str = "privateCar") -> dict:
        if (
            not isinstance(carpark_ids, list)
            or not 1 <= len(carpark_ids) <= 10
            or any(not isinstance(p, str) or not ID_PATTERN.fullmatch(p) for p in carpark_ids)
            or len(set(carpark_ids)) != len(carpark_ids)
        ):
            raise ValueError("Provide 1..10 unique, literal car park IDs from search_carparks")
        if not isinstance(vehicle_type, str) or vehicle_type not in VEHICLE_TYPES:
            raise ValueError("Unsupported vehicle type")
        snapshot = await self.catalog()
        known = {item["park_Id"]: item for item in snapshot.items}
        selected_ids = [park_id for park_id in carpark_ids if park_id in known]
        upstream = (
            await self.gateway.fetch("vacancy", carpark_ids=selected_ids, vehicle_type=vehicle_type)
            if selected_ids
            else None
        )
        by_id = {row["park_Id"]: row for row in upstream.records} if upstream else {}
        fetched_at = upstream.fetched_at if upstream else datetime.now(UTC)
        items = []
        for park_id in carpark_ids:
            if park_id not in known:
                items.append(
                    {"park_Id": park_id, "status": "not_found", "vehicle_type": vehicle_type, "records": []}
                )
                continue
            raw = by_id.get(park_id)
            vehicle_records = raw.get(vehicle_type) if raw else None
            if vehicle_records is not None and (
                not isinstance(vehicle_records, list) or any(not isinstance(r, dict) for r in vehicle_records)
            ):
                raise UpstreamError("Official parking API returned invalid vehicle records")
            from .vacancy import interpret_record

            records = [interpret_record(row, fetched_at) for row in vehicle_records or []]
            status = "missing_vacancy" if raw is None else "ok" if records else "missing_vehicle_records"
            items.append(
                {
                    **known[park_id],
                    "status": status,
                    "vehicle_type": vehicle_type,
                    "default_category": "HOURLY",
                    "records": records,
                }
            )
        return {
            "items": items,
            "fetched_at": fetched_at.isoformat(),
            "source_url": upstream.source_url if upstream else None,
            "freshness_policy": {
                "max_age_seconds": 300,
                "future_tolerance_seconds": 120,
                "naive_timestamp_timezone": "Asia/Hong_Kong",
            },
            "disclaimer": "Reported data is not a guarantee of availability on arrival.",
        }
