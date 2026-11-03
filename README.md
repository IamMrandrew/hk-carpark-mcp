# Hong Kong Parking MCP

Ask Grok to find Hong Kong car parks or on-street metered spaces and check reported availability.
The server uses the government parking API and signs you in through Pocket ID.

| Tool | What it does |
|---|---|
| `search_carparks` | Find car parks by Chinese or English name, address or district. |
| `get_carpark_vacancies` | Check vacancies and when the source last updated them. |
| `search_metered_spaces` | Find individual metered spaces by Chinese or English street, area or district. |
| `get_metered_occupancy` | Check reported occupancy for exact metered-space IDs. |

Try: **“Find car parks in Tsuen Wan and check their private-car vacancies.”**
Availability can change before you arrive. Missing or stale counts stay marked as unknown.

Try: **“Find metered parking on Island Road and check the reported occupancy.”**
Meter data comes from the [Transport Department's official dataset](https://data.gov.hk/en-data/dataset/hk-td-msd_1-metered-parking-spaces-data).
Location data is separate from occupancy. A meter's timestamp records its last
occupancy change, not its last sensor observation, so sensor freshness and
current availability remain unknown. Not-for-use meters are never shown as
vacant. Check signs, vehicle restrictions and charges before parking.

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
