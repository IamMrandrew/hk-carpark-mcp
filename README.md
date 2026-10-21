# Hong Kong Parking MCP

Ask Grok to find Hong Kong car parks and check reported vacancies.
The server uses the government parking API and signs you in through Pocket ID.

| Tool | What it does |
|---|---|
| `search_carparks` | Find car parks by Chinese or English name, address or district. |
| `get_carpark_vacancies` | Check vacancies and when the source last updated them. |

Try: **“Find car parks in Tsuen Wan and check their private-car vacancies.”**
Availability can change before you arrive. Missing or stale counts stay marked as unknown.

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
