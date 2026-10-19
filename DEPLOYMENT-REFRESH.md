# OAuth refresh fix

Pocket permits 60-minute access tokens and 30-day refresh sessions in the tested
installation. Its response omits `refresh_expires_in`. The bridge previously used
a one-hour fallback and retained that deadline through rotation.

The fix reads the matching active native session's expiry through the pinned,
read-only database contract. It supplies the remaining lifetime to the OAuth
library on initial issuance and rotation, honours shorter issuer metadata, and
rechecks the deadline after code delivery. Identity, scope and replay checks remain.

## Verification on 2026-10-18

- Original regression: about 3,599 seconds of bridge refresh validity for a 30-day native session.
- Native Pocket test: four rotations, expired-access recovery, restart, MCP and revocation passed against deployed code using temporary synthetic identities.
- A live government query returned 21 Tsuen Wan car parks.
- User reconnected Grok and completed a parking query. Aggregate production metadata recorded rotation with about 30 days remaining. No token values were selected.
- Public metadata returned 200; anonymous MCP returned 401. Overnight persistence remains a separate elapsed-time check.

## Repeat the native check

Install the checksum-pinned native binary separately and set `POCKET_TEST_BINARY`
to its absolute path. From the checkout:

```sh
CARPARK_TEST_LIVE_GOVERNMENT=1 uv run pytest -q -s tests/test_native_refresh_lifecycle.py
```

The test creates and removes its own synthetic Pocket identity state. It does not
use the production database, client secret or passkey account. With no binary set,
the optional native test skips.

## Deployment and rollback

See [DEPLOYMENT.md](DEPLOYMENT.md) for environment variables and the packaged entry
point. Earlier live code used flat modules; changing the package/launcher layout
requires the documented environment migration.

Set `CARPARK_BACKUP_DIR` privately to the source backup for the selected release.
Restore the previous source and compatible launcher as one set, then restart only
the MCP service. Preserve the existing environment, keys and token state. Do not
restore old token databases for routine code rollback: that could revive spent
refresh state. Database recovery needs a separate review.
