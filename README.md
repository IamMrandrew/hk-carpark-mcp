# Hong Kong Parking MCP

This server lets an MCP client look up Hong Kong car parks and their reported vacancies using the government API. It provides two read-only tools: `search_carparks` and `get_carpark_vacancies`.

The auth code connects MCP clients to Pocket ID v2.17.0 so users can sign in with a passkey. It is still under review. The parking tests pass, but the Pocket settings UI has known problems and the full Grok flow has not been verified.

## Run the checks

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run python scripts/live_mcp_check.py
```

The automated tests use synthetic identities and parking fixtures. The live check starts a temporary local server and calls the real government API. It uses a preview auth provider, so it does not test Pocket ID, passkeys or Grok.

One native lifecycle test skips because its binary harness is not included here. This repo also leaves out the Mac production launcher and private configuration. You can inspect and test the Python code, but cloning it is not enough to reproduce the running deployment. The packaged modules use relative imports; the current Mac deployment uses flat modules.

## How sign-in works

Grok connects through a Cloudflare Named Tunnel to a Python app on the home Mac. The app handles MCP requests and runs an OAuth bridge. Pocket ID is a separate local service that authenticates the user.

There are two client registrations. The bridge has a preconfigured OIDC client in Pocket, named Carpark MCP. Grok registers a separate OAuth client with the bridge. Pocket tokens stay on the server; Grok receives bridge tokens limited to `parking:read`.

[AUTH-REVIEW.md](AUTH-REVIEW.md) explains consent, refresh and the public route policy. It also lists the bugs that need fixing before this draft is ready to merge.

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

Production databases, passkeys, tokens, keys and logs are not included in this repository.
