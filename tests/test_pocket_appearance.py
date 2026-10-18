"""The sign-in style must not change native auth content or security headers."""
import asyncio
import re

import httpx
import pytest

from hkparking.auth.integration import PUBLIC, build_integration
from tests.test_fixes import crypto_setup


@pytest.mark.parametrize('path,content_type,content,styled', [
    ('/login', 'text/html; charset=utf-8', b'<head><script nonce="SYNTHETIC">boot()</script></head><form>Sign in</form>', True),
    ('/authorize', 'text/html', b'<head></head><button>Sign in</button>', True),
    ('/api/application-configuration', 'application/json', b'[{"key":"appName","value":"Pocket ID"}]', False),
])
def test_pocket_proxy_preserves_auth_document(tmp_path,path,content_type,content,styled):
    verifier, _token, _state, _db = crypto_setup(tmp_path)

    def native(request):
        return httpx.Response(200, content=content, headers={
            'content-type': content_type, 'content-security-policy': "script-src 'nonce-SYNTHETIC'",
            'set-cookie': 'SYNTHETIC-cookie=value; Secure; HttpOnly', 'etag': 'SYNTHETIC-etag'})

    app=build_integration(tmp_path/'bridge',verifier.config,'SYNTHETIC-secret',
                          pocket_transport=httpx.MockTransport(native))

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app),base_url=PUBLIC) as http:
            params = None
            if path == '/authorize':
                params={'client_id':verifier.config.client_id,'redirect_uri':PUBLIC+'/auth/callback',
                        'response_type':'code','scope':'openid','state':'SYNTHETIC-state',
                        'code_challenge':'x'*43,'code_challenge_method':'S256'}
            response=await http.get(path,params=params)
            assert response.status_code == 200
            assert response.headers['content-security-policy'] == "script-src 'nonce-SYNTHETIC'"
            assert 'Secure; HttpOnly' in response.headers['set-cookie']
            if styled:
                assert b'carpark-pocket-appearance' in response.content
                assert re.sub(rb'<style id="carpark-pocket-appearance">.*?</style>', b'', response.content, flags=re.DOTALL) == content
                assert 'etag' not in response.headers
            else:
                assert response.content == content
                assert response.headers['etag']=='SYNTHETIC-etag'
        await verifier.aclose()

    asyncio.run(run())
