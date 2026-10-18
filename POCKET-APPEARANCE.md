# Pocket ID sign-in appearance

The home Mac custom mountain image is removed from
`runtime/pocket/uploads/application-images/background.webp`, with a private copy
in the deployment backup. Pocket ID supplies its normal plain light/dark background.
This background setting applies to this Pocket ID instance, including other clients.

`appearance.py` adds a static system-font override for body/headings to native
HTML served at `/`, `/login`, `/interaction` and `/authorize`. This is bridge-side
presentation, not a Pocket ID binary modification. Native text colours, theme,
controls, script nonces, CSP and cookies remain intact. JSON and other routes are
unchanged. Validators for modified HTML are removed to avoid stale representations.

Verification: 291 tests passed, 2 skipped. Proxy tests check native document and
security-header preservation plus unchanged JSON. The pinned real Pocket fixture
checks login, code exchange, refresh/restart, MCP and revocation independently of
production identity state.

Rollback: restore integration.py from the deployment backup, remove appearance.py,
restore background.webp to its original upload path, and restart only the MCP job.
No identity/token database restore is needed for an appearance rollback.
