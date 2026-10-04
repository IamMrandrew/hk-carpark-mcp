# Contract — LAP-46

## Search
`search_carparks(query, district=null, limit=10, cursor=null)`

- query: 1..120 characters after rejecting blank input; case-insensitive whitespace-separated keyword AND matching across official bilingual names/addresses/districts. No fuzzy score, aliases, automatic selection or semantic logic. Preserve original IDs.
- district: optional 1..80-character keyword filter on bilingual districts.
- limit: integer 1..50. Deterministic ID sort; cursor binds query/district and catalog snapshot; reject malformed/obsolete cursors.
- response: `items[{park_Id,name_zh,name_en,address_zh,address_en,district_zh,district_en,opening_status}]`, `total`, `returned`, `has_more`, `next_cursor`, `catalog_fetched_at`, `source_urls`.
- Catalog: official zh_TW + en_US results merged by exact ID; cache at most 3600 seconds. Fetch failures after TTL fail closed, no silently served stale fallback.

## Vacancy
`get_carpark_vacancies(carpark_ids, vehicle_type="privateCar")`

- IDs: 1..10 unique literal IDs matching `[A-Za-z0-9_-]{1,64}`. Unknown well-formed IDs return per-ID `not_found`; no repair or case normalization. vehicle_type is one of privateCar/LGV/HGV/CV/coach/motorCycle.
- No vacancy cache. Fetch only known IDs from the fixed official upstream, then select the requested vehicle even if upstream also returns others.
- response: `items[{carpark metadata,status,vehicle_type,default_category="HOURLY",records:[...]}]`, `fetched_at`, `source_url`, `freshness_policy`, `disclaimer`.
- Missing upstream/vehicle records are explicit, never inferred from capacity. Preserve each category (including missing category); do not sum or silently treat unspecified records as HOURLY.

### Record interpretation
`interpret_record(raw, fetched_at, max_age_seconds=300, future_tolerance_seconds=120)` returns:
`category`, `vacancy_type`, `vacancy`, `lastupdate`, `lastupdate_iso`, `freshness`, `age_seconds`, `reported_status`, `reported_count`, `current_status`, `current_count`.

- A: integer >=0 → exact_count (zero means full). B: 1 available / 0 full, never an exact count. A/B -1 → missing_data. C: 0 closed. Unknown type → unknown_type; invalid type/value pairs (including bool/non-integer) → invalid_value. Missing raw fields are unknown, not zero.
- `reported_count` only valid type A; preserve raw fields, never fabricate them. C invalid values remain invalid_value.
- Parse naive timestamps as Asia/Hong_Kong by an explicit documented convention; preserve raw text and add ISO offset. Missing/invalid timestamps → freshness unknown. More than 120s in future → future_timestamp; small negative age → fresh with raw signed age.
- Fresh means -120 <= age <=300 seconds. Fresh records carry decoded `current_status/current_count`; stale/unknown/future records carry current_status unknown/current_count null, while keeping historical reported values.
- This is a freshness policy, not a guarantee of availability on arrival.

## Upstream boundaries
Only `GET https://api.data.gov.hk/v1/carpark-info-vacancy`. SSL verification on; redirects off; 10-second request timeout, 4 MiB response cap, validated JSON result list/IDs. No arbitrary URL, files, shell, third-party scraped data or LLM.

## Auth / test deployment
Andrew approved public parking exposure and replacing/cleaning up the old probe. The separate parking preview passed an authenticated public SDK→government API check; the old probe process/tunnel were then retired. The new origin needs its own connector authorization. Preserve the active parking preview's grants; don't restart its in-memory auth store casually. Production auth, fixed tunnel and Mac hosting remain LAP-47.
