# Home Mac OAuth refresh fix — 2026-10-18

The live Pocket client allows 60-minute access tokens and 43,200-minute (30-day) refresh sessions. The bridge used a one-hour fallback because Pocket omits `refresh_expires_in`. The SDK retained that original deadline across rotations. The old-code regression reproduced a refresh lifetime of about 3,599 seconds for a 30-day native session.

The fix reads the matching active native session's expiry through the existing pinned, read-only schema contract. It supplies the remaining lifetime to the OAuth library for initial issuance and rotation, honors shorter issuer metadata, and rechecks after code delivery. Five-minute bridge access tokens and all identity, scope, revocation and replay checks remain in place. An ambiguous upstream refresh failure still fails closed.

## Deployment

- Host: `100.116.248.28` (`home-macbook-pro-1`). SSH public host key matches the previously trusted `limacbook-pro.tail57555b.ts.net` identity.
- Repository base: `dc603ce8fa520c87544659a89fdb74d258ce9641`; local fix branch: `fix/oauth-refresh-reliability`.
- Changed live files: `/Users/lihome/.local/lib/hk-carpark-mcp/runtime/mcp/integration.py` and `native_status.py`. Relative imports were adapted to the existing flat-module deployment.
- Private backup: `/Users/lihome/.local/lib/hk-carpark-mcp/backups/oauth-refresh-20261018T115443Z`. It contains old modules, launcher/plist, a consistent stopped-MCP state copy and a read-only SQLite backup of Pocket. It stays on the home Mac.
- Only `com.laporatory.carpark-mcp` restarted. Pocket PID 40197 and tunnel PID 51304 remained unchanged at deployment verification.
- New SHA-256: `integration.py` = `4b16c79fb621e6d9230fd9badd83816c81a92fe775bbc817620ef5f801f044e8`; `native_status.py` = `793a303a0bab206531373e80e376a3e05afa010f88e5cbf3ef39ff4c53eb0da2`.

## Verification

- Local suite: 284 passed, 2 skipped; Ruff and diff whitespace checks passed. Skips are the absent legacy harness and the opt-in native test.
- Real Pocket v2.17.0 test passed on the Mac using fresh temporary synthetic identities. It verified about 2,591,999 seconds of refresh lifetime, four rotations, expired-access recovery, bridge restart recovery, MCP tools and revocation.
- The same test passed against the actual deployed flat modules and production parking module, with a live government query returning 21 Tsuen Wan car parks. The temporary native process and identity/token state were removed.
- Public OAuth metadata returned 200 through the existing Cloudflare hostname. Anonymous MCP returned 401. No credentials or production identities were copied to the development checkout.
- Existing Grok tokens had already expired. The user must reconnect once. A fresh real-client login and an overnight soak remain separate checks; native fixture results do not establish either.

To repeat the isolated test on the Mac from the staging directory:

```sh
cd /Users/lihome/.local/lib/hk-carpark-mcp/staging/oauth-refresh-20261018
CARPARK_TEST_FLAT_DIR=/Users/lihome/.local/lib/hk-carpark-mcp/runtime/mcp \
POCKET_TEST_BINARY=/Users/lihome/.local/lib/hk-carpark-mcp/bin/pocket-id-v2.17.0 \
CARPARK_TEST_LIVE_GOVERNMENT=1 PYTHONDONTWRITEBYTECODE=1 \
/Users/lihome/.local/lib/hk-carpark-mcp/runtime/mcp/.venv/bin/python \
  -m pytest -q -s tests/test_native_refresh_lifecycle.py
```

## Rollback

On the home Mac, restore only the two old source files and restart MCP:

```sh
cp /Users/lihome/.local/lib/hk-carpark-mcp/backups/oauth-refresh-20261018T115443Z/integration.py \
   /Users/lihome/.local/lib/hk-carpark-mcp/runtime/mcp/integration.py
cp /Users/lihome/.local/lib/hk-carpark-mcp/backups/oauth-refresh-20261018T115443Z/native_status.py \
   /Users/lihome/.local/lib/hk-carpark-mcp/runtime/mcp/native_status.py
launchctl kickstart -k "gui/$(id -u)/com.laporatory.carpark-mcp"
```

Do not restore an older token database during routine code rollback: that could restore spent refresh state. Keep the state backup for an explicitly reviewed recovery procedure.
