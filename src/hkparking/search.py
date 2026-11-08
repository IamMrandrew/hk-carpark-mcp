"""One bounded search across carparks and metered spaces, retaining source IDs."""

import asyncio
from itertools import zip_longest

from .metered import METERED_DISTRICT_FIELDS, METERED_TEXT_FIELDS, MeteredParkingService
from .service import (
    CARPARK_DISTRICT_FIELDS,
    CARPARK_TEXT_FIELDS,
    ParkingService,
    bounded_text,
    digest,
    matching_items,
    paginate,
)


class ParkingSearchService:
    def __init__(self, carparks: ParkingService, metered: MeteredParkingService):
        self.carparks, self.metered = carparks, metered

    async def search_parking(self, query, district=None, parking_type="all", limit=10, cursor=None):
        query = bounded_text(query, "query", 120)
        district = bounded_text(district, "district", 80, optional=True)
        if parking_type not in ("carpark", "metered", "all"):
            raise ValueError("parking_type must be carpark, metered or all")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 1024):
            raise ValueError("Invalid pagination cursor")
        selected = {}
        if parking_type in ("all", "carpark"):
            selected["carpark"] = (self.carparks, CARPARK_TEXT_FIELDS, CARPARK_DISTRICT_FIELDS)
        if parking_type in ("all", "metered"):
            selected["metered"] = (self.metered, METERED_TEXT_FIELDS, METERED_DISTRICT_FIELDS)
        # A failed selected source fails the search; never silently return an incomplete "all".
        snapshots = dict(
            zip(selected, await asyncio.gather(*(s[0].catalog() for s in selected.values())), strict=True)
        )
        matched = {}
        for kind, (_, text_fields, district_fields) in selected.items():
            rows = await asyncio.to_thread(
                matching_items, snapshots[kind].items, query, district, text_fields, district_fields
            )
            matched[kind] = rows
        # Round-robin across the complete matched streams, not independently per page.
        # Thus limit=1 and odd page sizes still alternate while both types have matches.
        merged = [
            (kind, row)
            for pair in zip_longest(matched.get("carpark", []), matched.get("metered", []))
            for kind, row in zip(("carpark", "metered"), pair, strict=True)
            if row is not None
        ]
        binding = digest(["search_parking", query, district, parking_type])
        fingerprint = digest({kind: snapshot.fingerprint for kind, snapshot in snapshots.items()})
        items, more, next_cursor = paginate(merged, limit, cursor, binding, fingerprint)
        items = [{"parking_type": kind, **row} for kind, row in items]
        return {
            "parking_type": parking_type,
            "items": items,
            "totals": {kind: len(rows) for kind, rows in matched.items()},
            "total": len(merged),
            "returned": len(items),
            "has_more": more,
            "next_cursor": next_cursor,
            "catalog_fetched_at": {
                kind: snapshot.fetched_at.isoformat() for kind, snapshot in snapshots.items()
            },
            "source_urls": [url for snapshot in snapshots.values() for url in snapshot.sources],
            "source_status": {kind: snapshot.source_status for kind, snapshot in snapshots.items()},
            "ordering": "Alternate carpark then metered, each in exact source-ID order; continue the remaining type when one ends.",
            "disclaimer": "Search gives locations, not availability or permission to park. Use the matching availability tool and check signs.",
        }
