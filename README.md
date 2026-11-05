# Hong Kong Parking MCP

Ask Grok to find Hong Kong car parks or on-street metered spaces and check reported availability.
The server uses the government parking API and signs you in through Pocket ID.

| Tool | What it does |
|---|---|
| `search_parking` | Preferred shared search; choose `carpark`, `metered` or `all` (default). |
| `search_carparks` | Compatibility carpark-only search; unchanged arguments and response. |
| `get_carpark_vacancies` | Check vacancies and when the source last updated them. |
| `search_metered_spaces` | Compatibility meter-only search; unchanged arguments and response. |
| `get_metered_occupancy` | Check reported occupancy for exact metered-space IDs. |

Try: **“Find car parks in Tsuen Wan and check their private-car vacancies.”**
Availability can change before you arrive. Missing or stale counts stay marked as unknown.

Try: **“Find metered parking on Island Road and check the reported occupancy.”**
Meter data comes from the [Transport Department's official dataset](https://data.gov.hk/en-data/dataset/hk-td-msd_1-metered-parking-spaces-data).
Location data is separate from occupancy. A meter's timestamp records its last
occupancy change, not its last sensor observation, so sensor freshness and
current availability remain unknown. Not-for-use meters are never shown as
vacant. Check signs, vehicle restrictions and charges before parking.

## Shared search interface

`search_parking(query, district=null, parking_type="all", limit=10, cursor=null)`

- `query`: required nonblank keyword string, at most 120 characters.
- `district`: optional nonblank district keywords, at most 80 characters.
- `parking_type`: `carpark`, `metered`, or `all`. Omission defaults to `all`.
- `limit`: integer 1–50, bounding the **total** items on the page, not each type.
- `cursor`: use the returned `next_cursor` with the same query, district and type.

The tool description asks the agent to choose explicitly: 「停車場」/ “car park”
means `carpark`; 「咪錶位」/「咪錶停車場」 means `metered`; a broad request such as
「附近有啲咩停車嘅地方」 means `all`. This is guidance for the agent, not a code
classifier. Location keywords are still needed; search does not calculate distance.

Each item has `parking_type` plus the original source fields: `park_Id` for a
carpark or `ParkingSpaceId` for a metered space. Use those IDs with the existing
`get_carpark_vacancies` or `get_metered_occupancy` tools; search is not availability.
The response also includes `totals` by selected type, `total`, `returned`,
`has_more`, `next_cursor`, per-type `catalog_fetched_at`, `source_urls`, `ordering`
and `disclaimer`. Meter totals count individual spaces, not streets or facilities.

All-mode alternates carpark then metered in each source's exact ID order, continuing
with the remaining type when one ends. This is balanced listing, not relevance
ranking. If a selected source fails, the whole search errors rather than silently
returning incomplete coverage. A single-type search does not query the other source.
Changed catalogue contents invalidate cursors. The two previous search tools remain
available for compatibility; no existing tool arguments or response fields changed.

## Connect Grok

Add a remote MCP connector using your server's public address:

| Field | Value |
|---|---|
| Server URL | `https://your-host/mcp` |
| Authentication | OAuth 2.0 Dynamic |
| Scope, if requested | `parking:read` |

Approve the Carpark request, then sign in with your Pocket ID passkey.
Grok handles token refresh. In the tested setup, each successful refresh renews a
30-day window; you should not need to sign in each day.

## Invite another user

Create an ordinary user through Pocket's private admin interface, then issue a
20-minute login code. Share its link privately so they can create their first
passkey on their own device. No password is needed. Email can be omitted only
when the instance's **Require User Email** policy is off; keep public signup disabled.

The bridge also requires an operator-approved enrollment grant for that code.
A Pocket login code alone is not enough. See [AGENTS.md](AGENTS.md#invitation-enrollment)
for the private operator steps and expiry rules.

To let the invited account use MCP, add its immutable Pocket user ID to
`CARPARK_ADDITIONAL_ALLOWED_SUBS` in the private server environment and restart.
The owner in `CARPARK_ALLOWED_SUB` stays allowed. Other Pocket users, including
onboarding test accounts, remain denied. Restart the connector's authorization
flow after changing access; do not reuse a failed consent page.

## Run your own server

You need Python 3.11–3.14, `uv`, Pocket ID v2.17.0, and a public HTTPS route to the
server. Pocket ID and the tunnel must already be configured.

1. Copy [`.env.example`](.env.example) to a private file outside Git and fill in your settings.
2. Set `CARPARK_ENV_FILE` to that file's path. Keep it private with `0600` permissions.
3. Start the server:

```sh
uv sync --locked
set -a
. "$CARPARK_ENV_FILE"
set +a
uv run python -m hkparking.auth
```

The server listens on loopback. Your tunnel provides public access.
For setup details or agent-assisted work, see [AGENTS.md](AGENTS.md).

## Project status

This is a personal project under review. Core sign-in and parking queries work on
the home Mac. The environment-based startup has passed staging tests; the live
host still uses its earlier launcher. Some auth hardening work remains.

To run the checks: `uv run pytest -q` and `uv run ruff check src tests`.
