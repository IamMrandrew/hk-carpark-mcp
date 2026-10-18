"""Refresh lifetimes follow the pinned Pocket session, not a one-hour guess."""
import asyncio
import base64
import hashlib
import sqlite3
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from mcp.server.auth.provider import TokenError

from hkparking.auth import integration as m
from tests.test_fixes import crypto_setup, downstream_client
from tests.test_refresh_gate import seed_refresh_session


def pocket_fixture(tmp_path, lifetime=30 * 86400):
    verifier, access_token, _state, path = crypto_setup(tmp_path)
    identity_transport = verifier.http._transport
    issued = []

    def issue(request):
        if request.url.path != '/api/oidc/token':
            return identity_transport.handler(request)
        form = parse_qs(request.content.decode())
        if form['grant_type'] == ['refresh_token']:
            assert form['refresh_token'] == [issued[-1]]
            assert form['scope'] == ['openid']
            with sqlite3.connect(path) as db:
                db.execute("UPDATE oauth2_sessions SET active=0 WHERE kind='refresh_token'")
        signature = f'SYNTHETIC-refresh-{len(issued)}'
        refresh = 'part.' + signature
        seed_refresh_session(path, verifier.config.allowed_sub, verifier.config.client_id,
                             signature, 'SYNTHETIC-native-grant', expires_in=lifetime)
        issued.append(refresh)
        return httpx.Response(200, json={
            'access_token': access_token({'jti': f'SYNTHETIC-access-{len(issued)}',
                                         'exp': int(time.time()) + 3600}),
            'refresh_token': refresh, 'token_type': 'Bearer', 'scope': 'openid',
            'expires_in': 3600,
            # Pocket does not include refresh_expires_in.
        })

    transport = httpx.MockTransport(issue)
    app = m.build_integration(tmp_path / 'bridge', verifier.config, 'SYNTHETIC-secret',
                              pocket_transport=transport)
    return app, verifier, issued, path, transport


async def initial_tokens(p, client, http, before_exchange=None):
    await p.register_client(client)
    async with p._upstream_oauth_client() as upstream:
        tokens = await upstream.fetch_token(url=m.PUBLIC + '/api/oidc/token',
                                             code='SYNTHETIC-upstream-code', scope='openid')
    verifier = 'SYNTHETIC-pkce-verifier-' + 'x' * 32
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    code = ClientCode(code='SYNTHETIC-code', client_id=client.client_id,
                      redirect_uri=str(client.redirect_uris[0]), code_challenge=challenge,
                      code_challenge_method='S256', scopes=m.SCOPE, idp_tokens=tokens,
                      expires_at=time.time() + 300, created_at=time.time())
    await p._code_store.put(key=code.code, value=code, ttl=300)
    if before_exchange:
        before_exchange()
    response = await http.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': client.client_id,
        'code': code.code, 'code_verifier': verifier, 'redirect_uri': str(client.redirect_uris[0]),
        'resource': m.RESOURCE,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_native_lifetime_survives_rotation_and_restart(tmp_path):
    lifetime = 30 * 86400
    app, verifier, issued, _path, transport = pocket_fixture(tmp_path, lifetime)
    client = downstream_client()

    async def run():
        p = app.state.provider
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=m.PUBLIC) as http:
            tokens = await initial_tokens(p, client, http)
            for _ in range(3):
                claims = p.jwt_issuer.verify_token(tokens['refresh_token'], expected_token_use='refresh')
                remaining = claims['exp'] - time.time()
                assert lifetime - 10 <= remaining <= lifetime, 'native refresh lifetime was truncated'
                old_refresh = tokens['refresh_token']
                response = await http.post('/token', data={
                    'grant_type': 'refresh_token', 'client_id': client.client_id,
                    'refresh_token': old_refresh,
                    'resource': m.RESOURCE,
                })
                assert response.status_code == 200, response.text
                tokens = response.json()
                assert tokens['refresh_token'] != old_refresh
                assert await p.load_access_token(tokens['access_token']) is not None
            assert len(issued) == 4
        await verifier.aclose()

        restarted = m.build_integration(tmp_path / 'bridge', verifier.config, 'SYNTHETIC-secret',
                                         pocket_transport=transport)
        p = restarted.state.provider
        async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(
                transport=httpx.ASGITransport(restarted), base_url=m.PUBLIC) as http:
            assert await p.load_access_token(tokens['access_token']) is not None
            response = await http.post('/token', data={
                'grant_type': 'refresh_token', 'client_id': client.client_id,
                'refresh_token': tokens['refresh_token'],
                'resource': m.RESOURCE,
            })
            assert response.status_code == 200, response.text
            current = response.json()
            # A spent refresh token must still revoke the family, including after restart.
            replay = await http.post('/token', data={
                'grant_type': 'refresh_token', 'client_id': client.client_id,
                'refresh_token': tokens['refresh_token'],
                'resource': m.RESOURCE,
            })
            assert replay.status_code == 401
            assert replay.json()['error'] == 'invalid_grant'
            assert await p.load_access_token(current['access_token']) is None

    asyncio.run(run())


def test_code_exchange_rechecks_native_deadline_after_delivery_delay(tmp_path):
    app, verifier, _issued, path, _transport = pocket_fixture(tmp_path)
    deadline = int(time.time()) + 120

    def shorten():
        with sqlite3.connect(path) as db:
            db.execute("UPDATE oauth2_sessions SET expires_at=? WHERE kind='refresh_token'", (deadline,))

    async def run():
        p = app.state.provider
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=m.PUBLIC) as http:
            tokens = await initial_tokens(p, downstream_client(), http, shorten)
            claims = p.jwt_issuer.verify_token(tokens['refresh_token'], expected_token_use='refresh')
            assert deadline - 3 <= claims['exp'] <= deadline
        await verifier.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('change', [
    "UPDATE oauth2_sessions SET active=0 WHERE kind='refresh_token'",
    "UPDATE oauth2_sessions SET expires_at=unixepoch('now')-1 WHERE kind='refresh_token'",
    "UPDATE users SET disabled=1",
    "UPDATE oauth2_sessions SET client_id='SYNTHETIC-other',request_data=json_set(request_data,'$.client_id','SYNTHETIC-other') WHERE kind='refresh_token'",
    "UPDATE oauth2_sessions SET request_data=json_set(request_data,'$.session.subject','SYNTHETIC-other-user') WHERE kind='refresh_token'",
])
def test_native_deadline_denies_inactive_or_mismatched_grants(tmp_path, change):
    app, verifier, _issued, path, _transport = pocket_fixture(tmp_path)
    p = app.state.provider

    async def run():
        async with app.router.lifespan_context(app), p._upstream_oauth_client() as upstream:
            tokens = await upstream.fetch_token(url=m.PUBLIC + '/api/oidc/token', code='SYNTHETIC-code')
            with sqlite3.connect(path) as db:
                db.execute(change)
            with pytest.raises(TokenError, match='invalid_grant'):
                p.normalize_native_tokens(tokens)
        await verifier.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('lifetime', [120, 30 * 86400])
def test_oauth_client_uses_native_expiry_when_response_omits_it(tmp_path, lifetime):
    app, verifier, _issued, _path, _transport = pocket_fixture(tmp_path, lifetime)
    p = app.state.provider

    async def run():
        async with app.router.lifespan_context(app), p._upstream_oauth_client() as upstream:
            tokens = await upstream.fetch_token(url=m.PUBLIC + '/api/oidc/token',
                                                 code='SYNTHETIC-code', scope='openid')
            assert lifetime - 2 <= tokens['refresh_expires_in'] <= lifetime
        await verifier.aclose()

    asyncio.run(run())
