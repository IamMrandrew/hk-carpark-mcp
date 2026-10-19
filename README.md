# Hong Kong Parking MCP

This server lets an MCP client look up Hong Kong car parks and their reported vacancies using the government API. It provides two read-only tools: `search_carparks` and `get_carpark_vacancies`.

The OAuth bridge connects Grok to Pocket ID v2.17.0 for passkey sign-in. This version is deployed on the home Mac. The user reconnected Grok and completed a parking query; live aggregate metadata also recorded refresh rotation with about 30 days of refresh validity. Overnight and long-term client persistence remain separate checks. Pocket administration and other known review gaps are listed in [AUTH-REVIEW.md](AUTH-REVIEW.md).

## Configure the deployment

Copy [`.env.example`](.env.example) to a private file and set the public origin, exact client callback, branding, ports and runtime paths. See [DEPLOYMENT.md](DEPLOYMENT.md) for the required settings and startup command. Deployment settings are read once at startup.

## Run the checks

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run python scripts/live_mcp_check.py
```

The automated tests use synthetic identities and parking fixtures. The live check starts a temporary local server and calls the real government API. It uses a preview auth provider, so it does not test Pocket ID, passkeys or Grok.

The optional native lifecycle test includes its synthetic identity harness but requires the separately installed, checksum-pinned Pocket ID binary. The older legacy-harness test also skips when its external module is absent. The private Mac launcher and configuration stay outside Git. A packaged environment-only entry point is now available; see [DEPLOYMENT.md](DEPLOYMENT.md). You can inspect and test the Python code, but cloning it is not enough to reproduce the running deployment. The packaged modules use relative imports; the current Mac deployment uses flat modules.

## How sign-in works

Grok connects through a Cloudflare Named Tunnel to a Python app on the home Mac. The app handles MCP requests and runs an OAuth bridge. Pocket ID is a separate local service that authenticates the user.

There are two client registrations. The bridge has a preconfigured OIDC client in Pocket, named Carpark MCP. Grok registers a separate OAuth client with the bridge. Pocket tokens stay on the server; Grok receives bridge tokens limited to `parking:read`.

[AUTH-REVIEW.md](AUTH-REVIEW.md) explains consent, refresh and the public route policy. It separates verified behaviour from remaining review and hardening work.

### Refresh lifetime

Bridge access tokens last five minutes. Clients renew them with a rotating refresh token; this should not require another passkey sign-in while the Pocket grant remains valid.

Pocket v2.17.0 does not include `refresh_expires_in` in its token response. The bridge now reads the matching active session's expiry from the pinned native database instead of imposing a one-hour fallback. It checks the owner, client, scope, active state and deadline. It also honors a shorter explicit issuer lifetime and rechecks the deadline when the authorization code is exchanged. The home Mac's current Pocket client permits 30-day refresh sessions.

Expired tokens issued before this fix cannot be revived. Reconnect once to obtain the corrected lifetime. Revocation, replay, upstream expiry and an ambiguous refresh failure can still require sign-in.

Set `POCKET_TEST_BINARY` to the approved binary path. An optional native lifecycle test uses a checksum-pinned Mac binary and a disposable synthetic Pocket identity. It tests code/PKCE/consent, access expiry, refresh rotation, bridge restart, MCP and revocation; it does not use production credentials or verify a real Grok session:

```sh
uv run pytest -q -s tests/test_native_refresh_lifecycle.py
```

Set `CARPARK_TEST_LIVE_GOVERNMENT=1` to include the real government API. See [DEPLOYMENT-REFRESH.md](DEPLOYMENT-REFRESH.md) for the deployed files and rollback.

## Where to review the code

- `src/hkparking/auth/integration.py` connects to Pocket, checks identity and decides which Pocket routes are reachable through the public hostname.
- `policy.py` checks OAuth request size, the Grok callback URL, scope, resource and PKCE requirements.
- `bridge.py` handles token issuance, refresh and local revocation using OAuthProxy hooks.
- `ledger.py` stores grant state, refresh reservations and the identity/client/grant binding.
- `native_status.py` reads Pocket's SQLite database to check current status against the pinned release and schema.
- `safe_logging.py` suppresses protocol log payloads that could contain credentials. It is not a guarantee that every log path is safe.

[CONTRACT.md](CONTRACT.md) describes the parking tools, input limits and vacancy interpretation.

## Parking data rules

The server calls only the fixed HTTPS government parking endpoint. It does not scrape other sites, accept arbitrary URLs, access files, run shell commands or use an LLM to rank matches.

Search results use literal keywords. The client must choose the correct car park; the server does not guess which result the user meant. Vacancy records keep their original categories and timestamps. The server does not sum categories or infer counts from capacity, and it does not present stale records as current availability.

## Current review snapshot

- Two read-only tools use the Hong Kong government car-park catalogue and vacancy API. No additional private/commercial feeds are included.
- Refresh lifetime follows the active native Pocket session instead of the old one-hour fallback. Normal successful rotations renew that native window.
- The Carpark consent page has minimal light/dark styling, escaped request labels, and the original SDK consent/CSRF checks. Its browser-tested referrer policy preserves the form Origin.
- Pocket sign-in uses the system font. The custom mountain image was backed up and removed on the host; Pocket supplies its plain background and native theme colours.
- Local verification on 2026-10-19: 309 passed, 2 skipped; Ruff passed. Earlier real Pocket lifecycle tests passed against the deployed modules. The environment refactor also passed the native lifecycle with a different origin/callback, including refresh/restart/revocation and a government query returning 21 Tsuen Wan car parks.
- Source, tests, lockfile and operational notes are versioned. The private production environment, launchd jobs, runtime state and image backup are host-managed. A new packaged entry point uses validated environment variables. This refactor is in the PR and has not changed the running host.

See [DEPLOYMENT-REFRESH.md](DEPLOYMENT-REFRESH.md), [CONSENT-UI.md](CONSENT-UI.md) and [POCKET-APPEARANCE.md](POCKET-APPEARANCE.md) for the changes and rollback boundaries. The latter also records the host-side background removal.

Production databases, passkeys, tokens, keys and logs are not included in this repository.
