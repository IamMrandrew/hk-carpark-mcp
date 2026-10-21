# Agent guide

Read [README.md](README.md) for the human overview and [`.env.example`](.env.example)
for setting names. Keep human instructions short. Put implementation constraints
here; add another Markdown file only when a separate audience or task needs it.
Code and tests are the authority when documenting exact behaviour.

## Work boundaries

- Preserve unrelated changes. Keep this PR draft and unmerged unless the user asks otherwise.
- A source push does not deploy the home Mac. Apply live changes only within the user's authorized scope.
- Back up host code, private environment, launcher and state before deployment changes. Keep backups on the host.
- Keep credentials, passkeys, keys, tokens, databases, logs and real host/user/path values outside Git. `.env.example` contains placeholders only; private `.env` files are ignored.
- Do not log token values, client secrets, callback query strings or authorization codes. `safe_logging.py` reduces protocol logging but is not proof that every logging path is safe.
- Keep ordinary visual rollback separate from identity/token database recovery. Restoring old token state can revive spent refresh tokens.

## Architecture

```text
Grok → HTTPS / Cloudflare → Python app on loopback
                            ├─ /mcp → government parking API
                            └─ OAuth bridge → Pocket ID on loopback
```

SSH/Tailscale is a separate management path. The bridge is a preconfigured private
OIDC client in Pocket; Grok registers a separate OAuth client with the bridge.
Pocket's Hidden Apps label means the native client has no Launch URL, not that it
is disabled. Do not expose management APIs to fix UI links that fail at the proxy.

| File under `src/hkparking/` | Responsibility |
|---|---|
| `deployment.py`, `config.py` | Validated startup settings and public/listener boundaries. |
| `app.py`, `tools.py` | MCP transport, token checks and the two tool registrations. |
| `client.py`, `service.py`, `vacancy.py` | Fixed government endpoint, search/cache and record interpretation. |
| `auth/runtime.py`, `auth/__main__.py` | Environment-only packaged entry point. |
| `auth/integration.py` | Pocket transport, identity checks, native route proxy and refresh lifetime. |
| `auth/policy.py`, `auth/bridge.py` | Request policy and SDK-backed OAuth operations. |
| `auth/ledger.py` | Grant identity binding, refresh reservations and revocation state. |
| `auth/native_status.py` | Pinned, read-only native database contract and file binding. |
| `auth/consent.py`, `auth/appearance.py` | Bridge consent HTML and native sign-in font styling. |

## Configure and start

Settings are read once per process. Restart after an environment change; use a
separate process for each deployment. The entry point does not load `.env` files.
Use the README startup command and source only a trusted private environment file.

- `CARPARK_PUBLIC_ORIGIN`: canonical HTTPS origin, without credentials, path, query, fragment or trailing slash. Startup requires it explicitly.
- `CARPARK_CLIENT_REDIRECT_URI`: one exact canonical HTTPS client callback; no wildcard, credentials or fragment. The default is Grok's official callback.
- `CARPARK_DISPLAY_NAME`, `CARPARK_SHORT_NAME`, `CARPARK_ICON_TEXT`: heading, short label and one-character icon. Request/config labels are HTML-escaped.
- `CARPARK_LISTEN_PORT`, `CARPARK_POCKET_PORT`: valid TCP ports; defaults `8767` and `14110`. Listen host remains `127.0.0.1`.
- `CARPARK_STATE_DIR`, `CARPARK_POCKET_DATABASE`, `CARPARK_POCKET_CLIENT_SECRET_FILE`: absolute canonical paths. Reuse existing state and secret files during migration.
- `CARPARK_POCKET_CLIENT_ID`, `CARPARK_ALLOWED_SUB`: existing private OIDC client and the immutable allowed owner subject.
- `CARPARK_ADDITIONAL_ALLOWED_SUBS`: optional JSON array of additional immutable Pocket user IDs; defaults to `[]`. Resolve approved usernames privately; never commit real IDs. Consent, access-token and refresh checks must all use this policy and preserve per-user grant binding.
- `CARPARK_POCKET_DB_DEVICE`, `CARPARK_POCKET_DB_INODE`: separately approved database file binding. Required; no guessed defaults.

The secret file must be regular, not a symlink, at most 4096 bytes, nonempty, and
private (`0600` or `0400`). Do not put its value in Git, environment examples or
command arguments. The runtime reads its path, then passes the secret in memory.

After verifying the Pocket database, get the two binding values with:

```sh
python - <<'PY'
import os
from pathlib import Path
s = Path(os.environ['CARPARK_POCKET_DATABASE']).stat()
print(f'CARPARK_POCKET_DB_DEVICE={s.st_dev}')
print(f'CARPARK_POCKET_DB_INODE={s.st_ino}')
PY
```

A database replacement/restore needs a binding review. Pocket release/schema
changes need explicit approval and compatibility checks; do not repin from a live
unknown database. Keep Pocket's public app URL equal to the configured bridge
origin. Its private client's callback must be `${CARPARK_PUBLIC_ORIGIN}/auth/callback`,
with `openid` and PKCE. Host, Origin, resource, issuer and native callback checks
must use the same configured origin, including any port.

`CARPARK_SSH_HOST`, `CARPARK_SSH_USER`, `CARPARK_INSTALL_DIR`,
`CARPARK_BACKUP_DIR` and `CARPARK_ENV_FILE` are optional operator/runbook values,
not Python server settings. Provisioning Pocket ID, the tunnel and service-manager
jobs is separate from this package. The current live host uses flat auth modules;
the PR adds a packaged startup path. Do not mix those import layouts during migration.
Earlier commits contain previous host metadata; current placeholders do not erase history.

## Invitation enrollment

Use the private Pocket admin API to create an enabled non-admin user and issue
`POST /api/users/{id}/one-time-access-token` with `{"ttl":"20m"}`. Never expose
these management endpoints through the public proxy. Check whether the user
already exists before creating one; never reset an existing passkey to retry.

Approve the returned code locally with `EnrollmentStore` from `auth/enrollment.py`,
using the running bridge's state directory and the user's immutable ID:

```python
# Run privately on the service host; values come from the authenticated admin API.
# Keep real codes out of command arguments, source files, logs and shell history.
store = EnrollmentStore(state_dir / "enrollment.sqlite")
store.approve(code, user_id, min(native_expiry, int(time.time()) + 1190))
assert store.pending(code) == user_id
store.close()
```

Share `${CARPARK_PUBLIC_ORIGIN}/lc/<code>?redirect=%2Fsignup%2Fadd-passkey`
privately. In Pocket v2.17.0, TTLs at most 15 minutes produce six-character codes;
the stock UI may require twelve, so use the explicit 20-minute TTL. The CLI
uses one hour and cannot set this expiry. Do not enable email recovery to work
around the code-length difference.

The bridge stores only hashes of the invitation code and native browser session.
It permits first enrollment only for an enabled non-admin user without existing
credentials, bound to the redeemed session and the invitation deadline. A finish
request reserves the grant atomically. Cancellation, invalid attestation or an
ambiguous failure leaves it blocked: inspect live credential state before issuing
a fresh invitation. Reissuing closes the previous grant; do not redeem the
recipient's code yourself as a test. Registering a passkey does not implicitly
change the MCP subject allowlist; include that explicit operator change when
provisioning an account intended to use MCP.

Public forwarding must always set an outgoing Cookie header from the current
request, even when empty. A shared HTTPX client otherwise reuses another browser's
cookies. Never fix this by clearing a shared cookie jar between requests.

## Auth constraints

1. Client discovery/registration leads to `/bridge/authorize` with state, S256 PKCE, the MCP resource and `parking:read`.
2. FastMCP creates the consent transaction and browser/CSRF state. The renderer only replaces successful HTML. Preserve cookies, error/redirect handling, double-submit CSRF and browser binding.
3. The bridge starts native Pocket authorization using its own client, upstream state and PKCE. Pocket signs in an explicitly allowed user with a passkey.
4. Pocket returns a code to `/auth/callback`. The bridge exchanges it and validates the allowed subject, client, scope and current native grant.
5. Grok receives a downstream code, then exchanges it with its PKCE verifier. Pocket tokens remain server-side; Grok receives bridge tokens for `parking:read`.

Bridge access lasts 300 seconds. Refresh reserves the previous pointer, checks the
live native grant, rotates upstream, verifies identity/client/grant binding and
records replacement downstream tokens. Replay revokes the local family. An
ambiguous failure/cancellation fails closed and can require reconnecting. Local
revocation does not tear down the upstream Pocket grant; do not invent RFC7009 support.

Pocket v2.17.0 omits `refresh_expires_in`. Derive it from the verified active native
session, preserve a shorter explicit issuer lifetime, and recheck after code
delivery. Do not use the one-hour fallback as the native session lifetime or renew
an expired grant. The tested native refresh window is 30 days, renewed by rotation.
The status reader is read-only, schema-pinned and device/inode-bound; preserve its
owner, enabled-user, client, scope and expiry checks. Do not broaden fixed scopes,
loopback listening, exact callback/origin validation or the government destination.

The outer Host/Origin boundary must run before state/network work. Keep
`Referrer-Policy: strict-origin` on consent HTML: `no-referrer` caused real browser
form POSTs to send `Origin: null`, which the boundary correctly rejected. Keep
null/foreign origins denied. Consent keeps no-store and frame protection.

## Parking tool contract

Only call `GET https://api.data.gov.hk/v1/carpark-info-vacancy`. Keep TLS verification,
no redirects, a 10-second timeout and a 4 MiB response cap. Validate JSON shape/IDs.
The parking tools do not accept arbitrary URLs, scrape other feeds, access files,
run shell commands or use an LLM to rank/select matches.

### `search_carparks(query, district=null, limit=10, cursor=null)`

- Nonblank query: 1–120 characters. Split on whitespace; every keyword must match a Chinese/English name, address or district, ignoring case. No fuzzy scores, aliases or automatic selection.
- Optional district: 1–80 characters. Limit: integer 1–50. Sort deterministically by exact source ID.
- Cursor binds query, district and catalogue snapshot; reject malformed/obsolete cursors.
- Merge `zh_TW` and `en_US` by exact ID. Cache for at most 3600 seconds; return an error if expired catalogue refresh fails.
- Return `items[{park_Id,name_zh,name_en,address_zh,address_en,district_zh,district_en,opening_status}]`, `total`, `returned`, `has_more`, `next_cursor`, `catalog_fetched_at`, `source_urls`.

### `get_carpark_vacancies(carpark_ids, vehicle_type="privateCar")`

- Accept 1–10 unique IDs matching `[A-Za-z0-9_-]{1,64}`. Preserve spelling/case; a well-formed unknown ID returns `not_found`.
- Vehicle classes: `privateCar`, `LGV`, `HGV`, `CV`, `coach`, `motorCycle`. Keep only the requested class. Do not cache vacancies.
- Return item metadata/status, `vehicle_type`, `default_category="HOURLY"`, records, `fetched_at`, `source_url`, `freshness_policy`, `disclaimer`.
- Preserve categories, including missing ones. Do not sum categories, infer from capacity or let `default_category` overwrite a record's category.

| Record | Interpretation |
|---|---|
| A, integer ≥0 | Exact count; zero means full. |
| B, 1 or 0 | Available or full; not an exact count. |
| A/B, -1 | `missing_data`. |
| C, 0 | `closed`; other C values are invalid. |
| Unknown type | `unknown_type`. |
| Invalid pair, boolean or noninteger | `invalid_value`; missing fields remain unknown. |

`interpret_record` preserves raw `category`, `vacancy_type`, `vacancy`, `lastupdate`,
plus `lastupdate_iso`, `freshness`, signed `age_seconds`, `reported_status`,
`reported_count`, `current_status`, `current_count`. Only valid A gives a reported
count. Interpret naive timestamps as `Asia/Hong_Kong`; preserve the original text.
Fresh age is -120 through 300 seconds inclusive. More future-dated records use
`future_timestamp`; missing/invalid times have unknown freshness. Stale/unknown/
future records keep reported values but expose unknown current status and null
current count. Never present missing/stale data as a current vacancy count.

## Appearance and rollback

Consent uses a centred compact app-to-server row, short icon labels, left-aligned
heading/description and neutral device-selected light/dark colours. Callback host
is visible; full callback, application ID and scopes are expandable. Use registered
client metadata, escaped labels and static local styles; no remote fonts/scripts.

Pocket's native sign-in HTML at `/`, `/login`, `/interaction`, `/authorize` gets
only a static system-font override. Keep native controls, light/dark colours, script
nonces, CSP and cookies. API/JSON responses are unchanged; remove validators when
HTML changes. The custom background was backed up and removed on the live host;
that appearance setting affects the entire Pocket instance, including other clients.

For rollback, restore the previous compatible code/launcher as a set and restart
only MCP. Restore the saved native background image if undoing that appearance
change. Keep existing state/keys/secrets and unrelated Pocket/tunnel services intact.
Do not restore token/identity databases for ordinary code or visual rollback.

## Checks and evidence

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src tests
uv run python scripts/live_mcp_check.py
```

The live check uses preview auth and the government API; it does not verify Pocket
or Grok. Preview grants are in-memory and disappear on restart; a new public origin
needs its own client authorization.

For the separate native lifecycle, set `POCKET_TEST_BINARY` to the checksum-pinned
macOS v2.17.0 binary, then run:

```sh
CARPARK_TEST_LIVE_GOVERNMENT=1 uv run pytest -q -s tests/test_native_refresh_lifecycle.py
```

It creates/removes temporary synthetic identities and state; do not substitute
production identities, credentials or databases. Without the binary it skips.
The older external legacy-harness test also skips when its module is absent.

Evidence snapshot, not a guarantee of current runtime health:

- 2026-10-19: 309 passed, 2 skipped; Ruff passed. A fresh process with alternate origin/callback checked refresh rotation/restart/replay, metadata, Host/Origin rejection and escaped branding.
- Native staged package with alternate origin/port/callback passed sign-in, four refreshes (~30-day validity), expired-access/restart recovery, MCP and revocation. Live lookup returned 21 Tsuen Wan car parks.
- 2026-10-18: user reconnected Grok and completed a query. Aggregate live metadata recorded refresh rotation and about 30 days remaining, without selecting token values. Public metadata was 200; anonymous MCP was 401.
- Consent Origin regression was reproduced/fixed in a real browser; mobile alignment and native Pocket login font were checked visually. Light-mode palette stayed native but was not separately checked visually.
- Core fixes/styles are deployed. The environment entry point is staging-tested and not yet applied to the live flat launcher. Overnight/long-term persistence and wider Tesla-specific behaviour remain unverified.

## Open work

- Anonymous DCR quota (16 registrations/24h) can be exhausted; keep this visible in review.
- Settings UI links do not imply supported management APIs. `/api/users/me` hits an earlier GET-only rule, making its later PUT rule unreachable. Catch-all GET/POST routing prevents allowed PATCH/DELETE actions.
- Reauthenticate/error methods are too broad; login/start permits POST even though the native route defines GET. A stale DCR comment says only `none`, but code also accepts `client_secret_post`.
- Add dedicated DCR/settings-route and broader authenticated management UI coverage. Do not claim a working public admin UI.
- Revoked-family retention/cleanup and upstream grant teardown remain open.
- Dependency advisory review, native WAL visibility and least-privilege operational testing remain open. Do not equate passed functional tests with a completed security review.
