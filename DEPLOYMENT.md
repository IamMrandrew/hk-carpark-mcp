# Configure the MCP server

The server reads deployment settings once, at process startup. Use a separate
process for each deployment. Restart the process after changing its environment.

```text
Private environment file → exported variables → validated settings → MCP server
                                                  └─ private secret file
```

## Set the environment

Copy `.env.example` to a private file outside the repository. Replace the examples
with your deployment values. Set its permissions to `0600`. Never commit that file.

| Variable | Purpose |
|---|---|
| `CARPARK_PUBLIC_ORIGIN` | Exact public HTTPS origin, without a path or trailing slash. |
| `CARPARK_CLIENT_REDIRECT_URI` | One exact HTTPS client callback. No wildcard or fragment. |
| `CARPARK_DISPLAY_NAME` | Name in the consent heading. |
| `CARPARK_SHORT_NAME` | Short label under the server icon. |
| `CARPARK_ICON_TEXT` | One-character server icon. Default: `P`. |
| `CARPARK_LISTEN_PORT` | Private MCP port. Default: `8767`. |
| `CARPARK_POCKET_PORT` | Private Pocket ID port. Default: `14110`. |
| `CARPARK_STATE_DIR` | Absolute path for bridge keys, tokens and ledger. |
| `CARPARK_POCKET_DATABASE` | Exact absolute path to the approved Pocket database. |
| `CARPARK_POCKET_DB_DEVICE`, `CARPARK_POCKET_DB_INODE` | Approved database file binding. Required; no guessed defaults. |
| `CARPARK_POCKET_CLIENT_ID` | Preconfigured private Pocket OIDC client. |
| `CARPARK_ALLOWED_SUB` | Immutable subject of the allowed owner. |
| `CARPARK_POCKET_CLIENT_SECRET_FILE` | Private file containing the existing client secret. |

The secret file must be a regular file with `0600` or `0400` permissions. A missing,
empty, shared-readable or symlinked secret file is rejected. Secret values do not
belong in `.env.example`, commands or Git.

Set the two database binding values only after verifying the database file. For
example, with `CARPARK_POCKET_DATABASE` already exported:

```sh
python - <<'PY'
import os
from pathlib import Path
s = Path(os.environ['CARPARK_POCKET_DATABASE']).stat()
print(f'CARPARK_POCKET_DB_DEVICE={s.st_dev}')
print(f'CARPARK_POCKET_DB_INODE={s.st_ino}')
PY
```

A database restore or replacement changes this binding. Review it before updating
the environment. The native release and schema remain pinned to Pocket ID v2.17.0.

## Start the server

Install the locked dependencies with `uv sync --locked`. Set `CARPARK_ENV_FILE` to
your private environment-file path. Source only a trusted file:

```sh
set -a
. "$CARPARK_ENV_FILE"
set +a
uv run python -m hkparking.auth
```

The new entry point replaces host-specific launch arguments. It does not load an
`.env` file automatically. It always listens on `127.0.0.1`; the tunnel supplies
public HTTPS. Access logs are disabled, and forwarded proxy headers are not trusted.
It does not install launchd jobs or change a tunnel or Pocket ID configuration.

Before changing a live installation, back up the existing private environment,
launcher and state on that host. Retain the existing state directory and secret
file. Do not regenerate them during this migration. Configure Pocket's client
callback as `${CARPARK_PUBLIC_ORIGIN}/auth/callback`, and keep Pocket's public app
URL consistent with the bridge origin. Keep the existing `openid` scope and PKCE.

## Host and backup paths

The example also names optional operator variables: `CARPARK_SSH_HOST`,
`CARPARK_SSH_USER`, `CARPARK_INSTALL_DIR`, `CARPARK_BACKUP_DIR`, and
`CARPARK_ENV_FILE`. They are for your launcher/runbook, not Python server settings.
For example:

```sh
ssh "${CARPARK_SSH_USER}@${CARPARK_SSH_HOST}"
```

Keep hostnames, usernames, real paths and backup files in that private environment.
Only placeholders are stored here. Earlier commits still contain previous host
metadata; replacing the latest files does not erase Git history.

## Fixed security boundaries

`parking:read`, the read-only government endpoint, loopback binding, the native
release/schema, PKCE, exact origin/callback checks and grant validation remain code
constraints. They are not unrestricted environment switches.

No production deployment is part of this configuration refactor. The existing host
continues to use its earlier launcher until this environment migration is applied.
