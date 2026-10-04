# Auth implementation review — NOT RELEASE APPROVED

## Ownership and architecture

Grok cloud → HTTPS Cloudflare Named Tunnel → Home Mac loopback Python app.
The app owns MCP resource `/mcp` and Bridge OAuth routes. Pocket ID runs as a separate loopback service.
Private device management is independent (Tailscale/SSH).

Two clients:
1. Pocket confidential OIDC client named Carpark MCP: Bridge → Pocket; fixed `/auth/callback`; openid.
2. Grok dynamically registered client: Grok → Bridge; fixed Grok callback; parking:read.
Grok DCR clients do not appear in Pocket's client table. A Pocket app is the user-facing representation of its OIDC client. Pocket v2.17.0 treats authorized apps without a Launch URL as hidden apps; this does not disable authorization.

## Why the allowlist exists

The intended public surface was MCP, identity login and OAuth consent, NOT Pocket administration/setup/enrollment/recovery. Outer Host/Origin checks and an explicit method/path allowlist enforce that boundary.
**Integration defect:** the complete native UI still exposes management links while `/api/users`, `/api/oidc/clients` and other admin APIs are denied. Admin role does not bypass this ingress boundary. A private management route or a deliberately reviewed public-admin design is needed; do not widen the entire API to silence errors.
Read-only loopback probe: unauthenticated native `/api/oidc/clients` = 401; same request through Bridge = 404. This proves the API is blocked, not the exact frontend error/empty-state mechanism.

## OAuth / consent

Grok discovery+DCR → Bridge authorize (state, S256 PKCE, resource, parking:read) → Bridge consent → Pocket authorize with preconfigured client (separate upstream state/PKCE, openid) → passkey/native authorization → Bridge callback/code exchange → owner/client/current-grant validation → downstream authorization code → Grok token exchange → Bridge access+refresh tokens.
Pocket tokens remain server-side. Bridge consent authorizes MCP capabilities; passkey authenticates identity. Native consent behavior follows Pocket policy.

## PKCE and client authentication

PKCE is not a substitute for client authentication. Policy requires S256 challenge on downstream authorization. Observed Grok DCR metadata requested client_secret_post and omitted scope. Candidate accepts none or client_secret_post, and absent or parking:read registration scope. Token exchange and client authentication remain SDK-owned.
Actual Grok PKCE/code exchange/automatic refresh acceptance is not established by this export. Registration alone is not proof of successful login.

## Refresh / revoke

Explicit downstream refresh validates client/scope/live pointer, reserves old refresh in ledger, checks native current grant, performs upstream refresh, validates provenance, and records rotated downstream tokens. Replayed refresh revokes local family; failed/cancelled refresh fails closed. Normal refresh requires no fresh passkey ceremony.
Local revoke is NOT implemented native Pocket upstream grant teardown. No native RFC7009 endpoint is invented.

## Known code/UI defects to review

- Public native management UI and denied admin APIs are inconsistent.
- Broad settings shell forwarding is not full settings acceptance.
- `/api/users/me` hits the earlier PUBLIC_PATHS branch, making the later PUT rule unreachable.
- PATCH/DELETE allowlist rules are not sufficient: catch-all router only admits GET/POST.
- Reauthenticate/error method rules are overly broad; login/start permits POST although native defines GET.
- Old policy comment says DCR admits only none; implementation also accepts client_secret_post.
- DCR compatibility additions and dashboard routing need explicit regression coverage.
- No browser-complete authenticated native UI/passkey/Grok acceptance suite.
- No reproducible packaged production launcher/native lifecycle harness in this export.
- Schema-pinned SQLite integration couples upgrades/restore to explicit approval and needs operational/WAL/least-privilege validation.
- Full dependency/advisory/security review and native upstream teardown remain gaps.

## Evidence / limits

Working-tree run before publication: 275 passed, 1 skipped, 1 Authlib deprecation warning; ruff clean. These are primarily synthetic checks and do NOT establish production readiness. Native isolated scratch results are historical and not rerun by this export.
Gzip fix strips content-encoding/length/transfer-encoding after decoded response buffering, avoiding double decompression. This does not establish all frontend assets/paths work.
No merge or production change is authorized by this draft. Private runtime/keys/DBs/logs and local operational AGENTS are excluded.
