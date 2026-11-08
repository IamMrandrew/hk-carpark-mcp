"""Opt-in bounded MCP integration for the unsupported HKeMobility web contract."""

import asyncio
from types import SimpleNamespace
from typing import Any

from .client import GovernmentClient, UpstreamError
from .hkemobility_source import ProbeError, read_park_details, run


class HKeMobilityService:
    def __init__(self, government: GovernmentClient):
        self.government = government
        self._lock = asyncio.Lock()

    async def inspect_park_ids(self, park_ids: list[str], language: str = "EN") -> dict[str, Any]:
        await self._lock.acquire()
        worker = asyncio.create_task(asyncio.to_thread(read_park_details, park_ids, language))

        def finished(task):
            self._lock.release()
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(finished)
        try:
            return await asyncio.shield(worker)
        except (ProbeError, ValueError, TypeError, KeyError) as error:
            raise UpstreamError("HKeMobility detail unavailable or contract invalid") from error

    async def inspect(self, ids: list[str], language: str = "EN") -> dict[str, Any]:
        if language not in ("EN", "TC"):
            raise ValueError("Unsupported language")
        # Blocking website reads stay off the serving loop, serialized to bound load.
        await self._lock.acquire()
        worker = asyncio.create_task(asyncio.to_thread(run, SimpleNamespace(
            epark_id=ids, language=language, timeout=10, stale_minutes=15,
        )))

        def finished(task):
            self._lock.release()
            # Retrieve late failures even if the MCP caller already cancelled.
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(finished)
        try:
            result = await asyncio.shield(worker)
        except (ProbeError, ValueError, TypeError, KeyError) as error:
            raise UpstreamError("HKeMobility source unavailable or contract invalid") from error
        official = await self.government.fetch("info", lang="en_US", carpark_ids=ids)
        # Government requests can broaden unexpectedly: never accept a non-exact ID.
        exact = [row for row in official.records if row["park_Id"] in ids]
        result["official_info"] = {
            "records": exact, "source_url": official.source_url,
            "fetched_at": official.fetched_at.isoformat(),
            "missing_ids": [i for i in ids if not any(r["park_Id"] == i for r in exact)],
        }
        closed_ids = {r["park_Id"].casefold() for r in exact if r.get("opening_status") == "CLOSED"}
        for rows in result["normalized"].values():
            for row in rows:
                if row["epark_id"].casefold() in closed_ids:
                    row["current_vacancy_count"] = None
                    row["flags"].append("official_info_reports_closed")
                    if row["vacancy_state"] in ("COUNT", "AVAILABLE"):
                        row["flags"].append("cross_source_opening_conflict")
        result["disclaimer"] = (
            "Experimental website contract, not a supported public API. "
            "Raw counts are not current availability. Capacity is not vacancy. "
            "Preserve unknowns, source times and conflicts; never guarantee parking on arrival."
        )
        return result
