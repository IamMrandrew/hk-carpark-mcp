"""Fixed-label OAuth diagnostics: never read request bodies, URLs or credentials."""
import json
import os
from datetime import UTC, datetime
from pathlib import Path

STEPS = {
    '/register': 'registration', '/bridge/authorize': 'authorization',
    '/token': 'token', '/revoke': 'revocation', '/consent': 'consent',
    '/auth/callback': 'upstream_callback',
    '/.well-known/oauth-authorization-server': 'oauth_discovery',
    '/.well-known/openid-configuration': 'oauth_discovery_alias',
}
MAX_LOG_BYTES = 1024 * 1024


class OAuthDiagnostics:
    def __init__(self, app, path):
        self.app = app
        self.path = Path(path)

    def record(self, step, method, status):
        # The private parent directory already belongs to bridge state. A full
        # sink or filesystem failure must not break the authentication flow.
        try:
            fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                os.fchmod(stream.fileno(), 0o600)
                if os.fstat(stream.fileno()).st_size < MAX_LOG_BYTES:
                    stream.write((json.dumps({
                        'utc': datetime.now(UTC).isoformat(),
                        'step': step, 'method': method if method in {'GET','POST','OPTIONS'} else 'other',
                        'status': status,
                    })+'\n').encode())
        except OSError:
            pass

    async def __call__(self, scope, receive, send):
        step = STEPS.get(scope.get('path')) if scope['type'] == 'http' else None
        if step is None:
            return await self.app(scope, receive, send)
        status = 500
        async def observed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = int(message['status'])
            await send(message)
        try:
            return await self.app(scope, receive, observed_send)
        finally:
            self.record(step, scope.get('method'), status)
