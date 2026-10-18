# Carpark consent page

The approved design replaces FastMCP's default consent HTML. It uses a compact,
centred application-to-Carpark visual, a left-aligned heading and description,
neutral colours, and device-selected light/dark appearance. The callback host is
visible; the full callback, application ID and requested scopes are expandable.
The requesting application name comes from the registered client, not a hardcoded
Grok label. Request-derived strings are HTML-escaped. No remote fonts, images or
scripts are loaded.

`PocketBridge._show_consent_page` first calls FastMCP 3.4.7's original handler. It
only replaces a successful consent HTML response. The existing response cookies
and security headers remain; content length is updated, with no-store and
no-referrer headers added. Error and redirect responses pass through. FastMCP
still owns transaction creation/expiry, CSRF issuance, double-submit verification,
Allow/Cancel handling and browser binding. Token issuance and refresh are unchanged.

Local verification: 288 passed, 2 skipped; Ruff passed. The focused tests cover
request-string escaping, approval, cancellation, missing browser-cookie rejection,
security headers and invalid transactions. The separate real Pocket lifecycle
check uses temporary synthetic identity state and the flat candidate modules.

Runtime files on the home Mac:

- `~/.local/lib/hk-carpark-mcp/runtime/mcp/integration.py`
- `~/.local/lib/hk-carpark-mcp/runtime/mcp/consent.py` (new)

The deployment backs up code and existing state privately on the host, then
restarts only `com.laporatory.carpark-mcp`. No Pocket ID or tunnel restart, database
restore, dependency change or credential change is needed. To roll back, restore
the backed-up integration.py and remove consent.py, then restart only the MCP job.
Do not restore an old token database as part of a visual rollback.
