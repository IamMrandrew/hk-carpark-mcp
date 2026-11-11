import asyncio
import json
import os

import httpx
from starlette.responses import JSONResponse

from hkparking.auth.oauth_diagnostics import MAX_LOG_BYTES, OAuthDiagnostics


def test_diagnostics_only_write_fixed_labels_status_and_time(tmp_path):
    path = tmp_path/'metadata.jsonl'
    async def handler(scope, receive, send):
        await JSONResponse({'client_secret':'SYNTHETIC-SECRET'},status_code=400)(scope,receive,send)
    app = OAuthDiagnostics(handler,path)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://example.test') as client:
            await client.post('/register?code=SYNTHETIC-QUERY',
                              headers={'Cookie':'SYNTHETIC-COOKIE','Authorization':'Bearer SYNTHETIC-TOKEN'},
                              json={'redirect_uris':['https://SYNTHETIC-PRIVATE.test/callback'],'client_secret':'SYNTHETIC-BODY'})
    asyncio.run(run())
    text = path.read_text()
    assert 'SYNTHETIC' not in text
    row = json.loads(text)
    assert set(row) == {'utc','step','method','status'}
    assert row['step'] == 'registration' and row['status'] == 400
    assert path.stat().st_mode & 0o777 == 0o600


def test_diagnostic_sink_is_bounded_and_does_not_follow_symlinks(tmp_path):
    path = tmp_path/'metadata.jsonl'
    app = OAuthDiagnostics(None,path)
    path.write_bytes(b'x'*MAX_LOG_BYTES)
    app.record('token','POST',400)
    assert path.stat().st_size == MAX_LOG_BYTES
    target = tmp_path/'target'
    target.write_text('unchanged')
    os.unlink(path)
    path.symlink_to(target)
    app.record('token','POST',400)
    assert target.read_text() == 'unchanged'
