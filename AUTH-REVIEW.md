# Reviewing the Pocket ID bridge

This review includes the deployed 2026-10-18 OAuth refresh fix, parking tools and sign-in styling. The personal deployment works in the tested scenarios; known hardening and deployment-reproducibility gaps remain below. Review publication is not merge approval.

The main design question is the public/private split. The bridge currently blocks Pocket's management APIs, but still serves a UI that links to them. That mismatch explains why an administrator can reach a page and still be unable to use it.

## What runs where

```text
Grok cloud MCP client
    |
    | HTTPS through Cloudflare Named Tunnel
    v
Home Mac: Python app, listening on loopback
    |-- OAuth bridge and grant ledger
    |-- /mcp parking tools --> government parking API
    |
    `-- Pocket ID, running as a separate loopback service
```

Device management uses Tailscale/SSH separately from the public MCP connection.

### The two client registrations

The bridge is an OIDC client of Pocket. Its preconfigured registration is named **Carpark MCP**, uses `/auth/callback`, and requests `openid`.

Grok is an OAuth client of the bridge. It registers dynamically with the bridge, uses the fixed Grok callback and requests `parking:read`. That registration does not appear in Pocket's client table.

Pocket's My Apps page is a user-facing view of OIDC clients. In v2.17.0, authorized clients without a Launch URL appear under Hidden Apps. That label does not disable the client or its authorization.

## Why management APIs are blocked

The public hostname was intended to provide MCP, sign-in and consent. It was not intended to expose Pocket administration, setup, passkey enrollment or recovery. The outer Host/Origin checks and the method/path allowlist enforce that split before requests reach Pocket.

I kept the complete Pocket UI available without providing a matching management route. That was an integration mistake: the UI offers actions that the proxy refuses. Being a Pocket administrator does not bypass the proxy.

A read-only check confirmed the difference. Without a login cookie, `/api/oidc/clients` returns 401 directly from Pocket, but 404 through the bridge. The management API is blocked. This check does not establish exactly how the frontend turns that response into an empty list or an Internal Error.

The next decision is whether to provide a private management entry point or explicitly support public administration. Opening every Pocket API just to remove UI errors would change the security boundary and needs a separate review.

## Sign-in and consent

The flow has two OAuth legs:

1. Grok discovers the bridge endpoints and registers a client.
2. The browser opens `/bridge/authorize` with state, an S256 PKCE challenge, the MCP resource and `parking:read`.
3. The user approves the bridge consent request.
4. The bridge starts Pocket authorization using its preconfigured client, `openid`, and separate upstream state and PKCE values.
5. Pocket authenticates the user with a passkey and applies its own authorization/consent policy.
6. Pocket sends a code to the bridge's `/auth/callback`. The bridge exchanges it for Pocket tokens and checks the owner, client and current grant.
7. The bridge sends a downstream authorization code to Grok's callback.
8. Grok exchanges that code, with its PKCE verifier, for bridge access and refresh tokens.

Passkey sign-in establishes identity. Bridge consent approves the MCP capability. Pocket tokens remain on the server; Grok only gets bridge tokens.

### PKCE and client authentication

PKCE protects the authorization code exchange. Client authentication identifies the client at the token endpoint. A flow can use both.

The observed Grok registration requested `client_secret_post` and omitted the registration scope. This candidate allows `none` or `client_secret_post`, and an absent scope or `parking:read`. Authorization still requires S256 PKCE. The SDK handles token exchange and client authentication.

On 2026-10-18 the user reconnected Grok and completed a parking query. A read-only aggregate live-state check showed a spent refresh pointer, an active replacement with about 30 days remaining, and new access tokens. This establishes real-client refresh rotation, separately from the synthetic native tests. Overnight and longer-term persistence are not yet established.

## Refresh and revocation

For an explicit refresh, the bridge checks the client, scope and live grant, then reserves the old refresh token in the ledger. It checks that the Pocket grant is still valid, refreshes upstream, verifies that the identity/client/grant binding has not changed, and records the new downstream tokens.

Reusing an old refresh token revokes the local token family. A failed or cancelled refresh also denies further use and requires a reconnect. A normal refresh does not need another passkey sign-in.

The 2026-10-18 refresh fix removes a lifetime mismatch. Pocket's native session can remain valid for 30 days, but its token response omits `refresh_expires_in`; the bridge previously substituted one hour and carried that deadline through rotations. `PocketOAuthClient` now supplies the verified native session lifetime to OAuthProxy. The code exchange rechecks the native deadline after any delivery delay. A shorter explicit issuer lifetime is preserved. This does not extend the actual native grant or relax its owner/client/scope, expiry, rotation or revocation checks. Already-expired downstream tokens require one reconnect.

Local revocation stops the bridge grant. It does **not** currently tear down the upstream Pocket grant. Pocket has no native RFC7009 endpoint in this integration, and the bridge does not invent one.

## Bugs visible in this draft

The route policy needs more work:

- Management links remain visible even though their APIs are blocked. Serving a settings page does not mean its actions work.
- `/api/users/me` matches the earlier `PUBLIC_PATHS` branch, so the later PUT rule is unreachable.
- The allowlist contains PATCH/DELETE rules, but the catch-all router only accepts GET/POST.
- The reauthenticate/error rules accept too many methods. The login/start rule also permits POST even though Pocket defines GET.
- A policy comment still says DCR accepts only `none`; the code also accepts `client_secret_post`.

The DCR compatibility changes and settings routes need dedicated regression tests. Broader authenticated Pocket management UI coverage remains open. Real Grok sign-in/use and a live refresh rotation are now verified as described above; this does not prove every Grok/Tesla client scenario.

Deployment and security work remains too. The repo does not include a reproducible production launcher. The native lifecycle harness is now included in tests/test_native_refresh_lifecycle.py; its pinned Pocket binary remains an external prerequisite. The SQLite status check is tied to a specific release and schema, so upgrades and database restores need explicit approval. WAL visibility and least-privilege access still need operational testing. A full dependency/advisory review and upstream grant teardown are also unfinished.

## Current verification — 2026-10-18

- Local suite: **291 passed, 2 skipped**; Ruff passed. Skips are the absent older legacy harness and the opt-in pinned native test, which ran separately on the Mac.
- Real Pocket v2.17.0 lifecycle on disposable synthetic identities passed against staged and actual deployed modules: native sign-in/code exchange, four rotations, expired-access recovery, bridge restart, MCP and revocation. With the government API enabled, the deployed code returned 21 Tsuen Wan car parks.
- Public OAuth metadata returned 200 and anonymous MCP returned 401. Existing production token state was retained across deployments.
- The minimal consent page keeps SDK transaction/CSRF/browser-binding checks. Real-browser testing reproduced Origin:null under no-referrer and verified the fix using strict-origin; null/foreign origins remain denied.
- Live Pocket login was visually checked at mobile width after removing the custom mountain image and adding system-font styling. Native CSP/nonces/cookies remain unchanged. This is presentation verification, not a new manual passkey login on that browser.
- Overnight persistence, broader Tesla-specific behaviour and the hardening gaps above remain outside these checks.

## Earlier publication snapshot (historical)

The publication checkout passed **275 tests**, with **1 skipped** and **1 Authlib deprecation warning**. Ruff passed. The run used the existing pinned local virtual environment with this checkout's `src` on `PYTHONPATH`, rather than a fresh installation.

Most tests use synthetic fixtures. They do not establish that the production passkey/Grok/Tesla flow works. Earlier isolated native runs are historical evidence and were not rerun for this export.

The gzip fix removes content-encoding, content-length and transfer-encoding headers after buffering the decoded response. That avoids decoding the same body twice. It does not prove that every frontend asset or route works.

This draft excludes private runtime files, keys, databases, logs, operational AGENTS and the production launcher. Publishing or editing the draft does not deploy it or authorize a merge.
