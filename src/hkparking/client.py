"""Read-only gateway to exactly one government HTTPS endpoint."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

ENDPOINT = "https://api.data.gov.hk/v1/carpark-info-vacancy"
VEHICLE_TYPES = ("privateCar", "LGV", "HGV", "CV", "coach", "motorCycle")
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class UpstreamError(RuntimeError):
    """Safe message suitable for a tool error, never an upstream body or secret."""


@dataclass(frozen=True)
class UpstreamResult:
    records: list[dict]
    source_url: str
    fetched_at: datetime


class GovernmentClient:
    def __init__(
        self, http: httpx.AsyncClient, timeout_seconds: float = 10, max_bytes: int = 4 * 1024 * 1024
    ):
        if timeout_seconds <= 0 or max_bytes <= 0:
            raise ValueError("Positive upstream limits required")
        self.http = http
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    async def fetch(
        self,
        data: str,
        *,
        lang: str = "zh_TW",
        carpark_ids: list[str] | None = None,
        vehicle_type: str | None = None,
    ) -> UpstreamResult:
        if data not in ("info", "vacancy") or lang not in ("zh_TW", "en_US"):
            raise ValueError("Unsupported government API operation/language")
        params = {"data": data, "lang": lang}
        if carpark_ids is not None:
            if not 1 <= len(carpark_ids) <= 10 or any(
                not isinstance(i, str) or not ID_PATTERN.fullmatch(i) for i in carpark_ids
            ):
                raise ValueError("Require 1..10 valid literal car park IDs")
            params["carparkIds"] = ",".join(carpark_ids)
        if vehicle_type is not None:
            if vehicle_type not in VEHICLE_TYPES:
                raise ValueError("Unsupported vehicle type")
            params["vehicleTypes"] = vehicle_type
        url = str(httpx.URL(ENDPOINT, params=params))
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self.http.stream(
                    "GET", url, follow_redirects=False, timeout=self.timeout_seconds
                ) as response:
                    if response.status_code != 200:
                        raise UpstreamError("Official parking API unavailable (HTTP status error)")
                    chunks = []
                    size = 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > self.max_bytes:
                            raise UpstreamError("Official parking API response exceeds size limit")
                        chunks.append(part)
            payload = json.loads(b"".join(chunks))
        except UpstreamError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise UpstreamError("Official parking API request failed or timed out") from None
        except (ValueError, UnicodeDecodeError):
            raise UpstreamError("Official parking API returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise UpstreamError("Official parking API returned an invalid result shape")
        seen = set()
        for row in payload["results"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("park_Id"), str)
                or not ID_PATTERN.fullmatch(row["park_Id"])
            ):
                raise UpstreamError("Official parking API returned an invalid car park record")
            if row["park_Id"] in seen:
                raise UpstreamError("Official parking API returned duplicate car park IDs")
            seen.add(row["park_Id"])
        return UpstreamResult(payload["results"], url, datetime.now(UTC))
