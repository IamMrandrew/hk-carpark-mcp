# Parking tool contract

These tools return official parking data without choosing a car park for the user or guessing missing vacancy counts.

## Search for car parks

```text
search_carparks(query, district=null, limit=10, cursor=null)
```

`query` must contain 1..120 characters and cannot be blank. Search splits it on whitespace and requires every keyword to match an official Chinese or English name, address or district. Matching ignores case. It does not use fuzzy scores, aliases or semantic ranking, and it does not automatically select a result.

The optional `district` filter accepts 1..80 characters and searches bilingual district names. `limit` must be an integer from 1..50. Results sort deterministically by ID. A cursor is tied to the query, district and catalog snapshot; malformed or obsolete cursors are rejected.

The response contains:

- `items[{park_Id,name_zh,name_en,address_zh,address_en,district_zh,district_en,opening_status}]`
- `total`, `returned`, `has_more`, `next_cursor`
- `catalog_fetched_at`, `source_urls`

The catalog merges official `zh_TW` and `en_US` results by exact ID and caches them for at most 3600 seconds. If a refresh fails after that period, the tool returns an error rather than silently serving an expired catalog. IDs are preserved as supplied by the source.

## Get vacancies

```text
get_carpark_vacancies(carpark_ids, vehicle_type="privateCar")
```

Supply 1..10 unique IDs matching `[A-Za-z0-9_-]{1,64}`. The tool does not repair IDs or change their case. A validly formatted but unknown ID returns `not_found` for that item.

`vehicle_type` must be one of `privateCar`, `LGV`, `HGV`, `CV`, `coach` or `motorCycle`. The tool fetches known IDs from the fixed government endpoint and keeps only the requested vehicle class, even if the response includes others. Vacancy results are not cached.

The response contains:

- `items[{carpark metadata,status,vehicle_type,default_category="HOURLY",records:[...]}]`
- `fetched_at`, `source_url`, `freshness_policy`, `disclaimer`

Missing car park or vehicle records stay explicit. The tool does not infer vacancies from capacity. Each record keeps its category, including a missing category; the tool neither sums categories nor silently labels an unspecified record as HOURLY. The `default_category` field does not override those record categories.

### Reading a vacancy record

```text
interpret_record(raw, fetched_at, max_age_seconds=300, future_tolerance_seconds=120)
```

The result includes `category`, `vacancy_type`, `vacancy`, `lastupdate`, `lastupdate_iso`, `freshness`, `age_seconds`, `reported_status`, `reported_count`, `current_status` and `current_count`.

The government record types mean:

- **A:** an integer of zero or more is an `exact_count`; zero means full.
- **B:** 1 means available and 0 means full. Neither is an exact vacancy count.
- **A or B with -1:** `missing_data`.
- **C with 0:** `closed`. Other C values remain `invalid_value`.

Unknown types return `unknown_type`. Invalid type/value pairs, including booleans and non-integers, return `invalid_value`. Missing raw fields mean unknown, not zero. Only a valid type A record supplies `reported_count`; raw fields are preserved rather than fabricated.

### Deciding whether a record is current

Naive timestamps are interpreted as `Asia/Hong_Kong` by the tool's documented convention. The original text is retained and an offset-aware ISO timestamp is added.

A record is fresh when its age is between -120 and 300 seconds, inclusive. A small negative age is allowed, and its signed value is retained. A timestamp more than 120 seconds in the future gets `future_timestamp`; missing or invalid timestamps get unknown freshness.

Fresh records expose their decoded `current_status` and `current_count`. Stale, unknown or excessively future-dated records use `current_status="unknown"` and `current_count=null`, while retaining their historical reported values. This policy does not guarantee a space will still be available when the driver arrives.

## Upstream limits

The only upstream request is `GET https://api.data.gov.hk/v1/carpark-info-vacancy`. TLS verification is enabled and redirects are disabled. Requests have a 10-second timeout and a 4 MiB response cap. The response must be valid JSON with the expected result list and IDs.

The tools do not accept arbitrary URLs, read files, run shell commands, scrape third-party data or call an LLM.

## Preview checks and production auth

A previous temporary parking preview passed an authenticated public SDK-to-government-API check. That is historical preview evidence, not proof that the Pocket bridge or Grok works. A new origin needs its own connector authorization, and restarting the preview's in-memory auth store loses its grants.

The Pocket bridge and its remaining deployment work are described in [AUTH-REVIEW.md](AUTH-REVIEW.md). They are separate from this parking data contract.
