# Hong Kong Parking MCP — review candidate

Read-only government parking tools, plus a Pocket ID v2.17.0 OIDC-to-MCP OAuth bridge. **Draft, not release-approved.**

## Checks

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run python scripts/live_mcp_check.py
```

Unit fixtures are synthetic. The live check uses an isolated ephemeral preview provider and the government API; it does **not** certify Pocket/passkey/Grok integration. One native lifecycle test skips because the private binary harness is not included. No production account data, keys, DBs or runtime files are shipped.

## Review entry points

- `src/hkparking/auth/integration.py`: Pocket loopback transport, verifier, route sharing and public allowlist.
- `src/hkparking/auth/policy.py`: DCR metadata bounds, Grok callback, resource/scope and PKCE admission.
- `src/hkparking/auth/bridge.py`: OAuthProxy hooks, issuance, explicit refresh, local revoke.
- `src/hkparking/auth/ledger.py`: durable grant/reservation/provenance state.
- `src/hkparking/auth/native_status.py`: read-only, release/schema/file-bound Pocket status checks.
- `src/hkparking/auth/safe_logging.py`: protocol payload suppression.
- `CONTRACT.md`: parking data semantics and bounds.
- `AUTH-REVIEW.md`: architecture, rationale, known defects and acceptance gaps.

Production uses a separate Mac launcher; this export intentionally contains no private launcher/config. Package imports differ from the flat modules used in that deployment. The repository is not a turn-key deployment and must not be presented as one.

## Parking tool scope

Only the fixed HTTPS government parking endpoint. No shell, files, arbitrary URLs, LLM ranking or scraping. Preserve category/type/time, do not infer capacities or sum categories, and suppress stale current-availability claims.
