"""Opt-in real Pocket v2.17.0 lifecycle using only a fresh synthetic identity.

Set POCKET_TEST_BINARY to the pinned macOS binary. No production credentials,
database or browser session are used. The native process/state are temporary.
"""
import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

if os.environ.get('CARPARK_TEST_FLAT_DIR'):
    sys.path.insert(0, os.environ['CARPARK_TEST_FLAT_DIR'])
    from integration import PUBLIC, RELEASE, PocketConfig, build_integration
else:
    from hkparking.auth.integration import PUBLIC, RELEASE, PocketConfig, build_integration

MAC_BINARY_SHA256 = 'eaf433ae4b8e810bd7af8cf071144cb555947e929c51c115b30ce2b2c29b778d'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def native_fixture(home):
    binary = os.environ.get('POCKET_TEST_BINARY')
    if not binary:
        pytest.skip('set POCKET_TEST_BINARY to run the isolated native lifecycle')
    binary = Path(binary).resolve()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == MAC_BINARY_SHA256
    home.mkdir(mode=0o700)
    port = free_port()
    database = home / 'pocket.db'
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'APP_URL': PUBLIC,
           'HOST': '127.0.0.1', 'PORT': str(port), 'ACTORS_HOST': '127.0.0.1',
           'ACTORS_PORT': str(free_port()), 'ENCRYPTION_KEY': secrets.token_urlsafe(32),
           'DB_CONNECTION_STRING': str(database), 'ANALYTICS_DISABLED': 'true',
           'VERSION_CHECK_DISABLED': 'true', 'TRUST_PROXY': '127.0.0.1/32',
           'LOG_LEVEL': 'error', 'LOG_QUERY_ARGS': 'false'}
    process = subprocess.Popen([str(binary)], cwd=home, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        headers = {'host': 'carpark.laporatory.com', 'x-forwarded-proto': 'https'}
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers=headers,
                          trust_env=False, timeout=2) as http:
            deadline = time.monotonic() + 60
            while True:
                assert process.poll() is None, 'isolated Pocket process exited'
                try:
                    if http.get('/.well-known/openid-configuration').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, 'isolated Pocket startup timed out'
                time.sleep(.1)
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO users(id,username,first_name,last_name,display_name,is_admin,disabled) VALUES('fixture-sub','SYNTHETIC-user','SYNTHETIC','','SYNTHETIC',1,0)")
            # Recovery is for this disposable synthetic user only; output stays in memory.
            recovery = subprocess.run([str(binary), 'one-time-access-token', 'SYNTHETIC-user'],
                                      cwd=home, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, timeout=15, check=True)
            match = re.search(rb'/lc/([A-Za-z0-9_-]+)', recovery.stdout)
            assert match is not None, 'synthetic recovery response incompatible'
            assert http.post('/api/one-time-access-token/' + match.group(1).decode()).status_code == 200
            del recovery, match
            # The native cookie is Secure. This private loopback HTTP fixture
            # represents the trusted TLS proxy, so forward its cookie explicitly.
            http.headers['cookie'] = '; '.join(k + '=' + v for k, v in http.cookies.items())
            response = http.post('/api/oidc/clients', json={
                'id': 'fixture-client', 'name': 'SYNTHETIC-client',
                'callbackURLs': [PUBLIC + '/auth/callback'], 'logoutCallbackURLs': [],
                'isPublic': False, 'pkceEnabled': True, 'requiresReauthentication': False,
                'requiresPushedAuthorizationRequests': False, 'skipConsent': False,
                'isGroupRestricted': False, 'credentials': {},
                'accessTokenDurationMinutes': 60, 'refreshTokenDurationMinutes': 43200,
            })
            assert response.status_code == 201
            client = response.json()
            yield {'database': database, 'port': port, 'client': client['id'],
                   'secret': client['createdSecret']['secret'], 'cookies': dict(http.cookies)}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def sign_in(http):
    metadata = {'redirect_uris': ['https://grok.com/connectors-oauth-exchange-code/'],
                'token_endpoint_auth_method': 'none', 'scope': 'parking:read',
                'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code']}
    response = await http.post('/register', json=metadata)
    assert response.status_code == 201
    client = response.json()['client_id']
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    response = await http.get('/bridge/authorize', params={
        'client_id': client, 'redirect_uri': metadata['redirect_uris'][0], 'response_type': 'code',
        'scope': 'parking:read', 'resource': PUBLIC + '/mcp', 'state': 'SYNTHETIC-state',
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    })
    assert response.status_code == 302
    page = await http.get(response.headers['location'])
    assert page.status_code == 200

    def field(name):
        return html.unescape(re.search('name="' + name + r'"\s+value="([^"]+)"', page.text).group(1))

    response = await http.post('/consent', data={
        'txn_id': field('txn_id'), 'csrf_token': field('csrf_token'), 'action': 'approve'})
    assert response.status_code == 302
    response = await http.get(response.headers['location'])
    assert response.status_code == 302
    interaction = parse_qs(urlsplit(response.headers['location']).query)['interaction'][0]
    response = await http.get('/api/oidc/interactions/' + interaction)
    assert response.status_code == 200
    for step in response.json()['requiredSteps']:
        response = await http.post('/api/oidc/interactions/' + interaction + '/complete', json={'step': step})
        assert response.status_code == 200
    response = await http.get(response.json()['redirectUrl'])
    assert response.status_code in (302, 303)
    response = await http.get(response.headers['location'])
    assert response.status_code == 302
    code = parse_qs(urlsplit(response.headers['location']).query)['code'][0]
    response = await http.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': client, 'code': code,
        'code_verifier': verifier, 'redirect_uri': metadata['redirect_uris'][0], 'resource': PUBLIC + '/mcp'})
    assert response.status_code == 200
    return client, response.json()


async def mcp_list(http, access):
    response = await http.post('/mcp', headers={
        'Authorization': 'Bearer ' + access, 'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    return response


@pytest.fixture
def private_native_root(tmp_path):
    with tempfile.TemporaryDirectory(prefix='SYNTHETIC-refresh-', dir=tmp_path) as home:
        os.chmod(home, 0o700)
        yield Path(home)


def test_real_native_refresh_expiry_rotation_and_restart(private_native_root):
    tmp_path = private_native_root
    with native_fixture(tmp_path / 'native') as fixture:
        path = fixture['database']
        st = path.stat()
        cfg = PocketConfig('fixture-sub', fixture['client'], fixture['port'], str(path),
                           RELEASE, (st.st_dev, st.st_ino))

        def new_app():
            app = build_integration(tmp_path / 'bridge', cfg, fixture['secret'])
            app.state.provider._fastmcp_access_token_expiry_seconds = 2
            return app

        async def run():
            app = new_app()
            government_verified = False
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=PUBLIC, cookies=fixture['cookies']) as http:
                client, tokens = await sign_in(http)
                p = app.state.provider
                claims = p.jwt_issuer.verify_token(tokens['refresh_token'], expected_token_use='refresh')
                assert claims['exp'] - time.time() > 86400, 'real native lifetime truncated to one hour'
                response = await mcp_list(http, tokens['access_token'])
                assert response.status_code == 200
                assert {t['name'] for t in response.json()['result']['tools']} == {'search_carparks', 'get_carpark_vacancies'}
                if os.environ.get('CARPARK_TEST_LIVE_GOVERNMENT') == '1':
                    result = await http.post('/mcp', headers={
                        'Authorization': 'Bearer ' + tokens['access_token'],
                        'Accept': 'application/json, text/event-stream'},
                        json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                              'params': {'name': 'search_carparks', 'arguments': {'query': '荃灣'}}})
                    assert result.status_code == 200
                    body = result.json()['result']
                    assert not body.get('isError', False)
                    assert body['structuredContent']['total'] > 0
                    government_verified = True
                await asyncio.sleep(3)
                assert (await mcp_list(http, tokens['access_token'])).status_code == 401
                response = await http.post('/token', data={
                    'grant_type': 'refresh_token', 'client_id': client, 'refresh_token': tokens['refresh_token'],
                    'scope': 'parking:read', 'resource': PUBLIC + '/mcp'})
                assert response.status_code == 200
                tokens = response.json()
                assert (await mcp_list(http, tokens['access_token'])).status_code == 200
            # New provider, disk stores, native verifier and HTTP clients; same saved grant.
            app = new_app()
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=PUBLIC) as http:
                for _ in range(3):
                    response = await http.post('/token', data={
                        'grant_type': 'refresh_token', 'client_id': client, 'refresh_token': tokens['refresh_token'],
                        'scope': 'parking:read', 'resource': PUBLIC + '/mcp'})
                    assert response.status_code == 200
                    current = response.json()
                    assert current['refresh_token'] != tokens['refresh_token']
                    tokens = current
                    claims = app.state.provider.jwt_issuer.verify_token(tokens['refresh_token'], expected_token_use='refresh')
                    assert claims['exp'] - time.time() > 86400
                    assert (await mcp_list(http, tokens['access_token'])).status_code == 200
                response = await http.post('/revoke', data={
                    'client_id': client, 'token': tokens['refresh_token'], 'token_type_hint': 'refresh_token'})
                assert response.status_code == 200
                assert (await mcp_list(http, tokens['access_token'])).status_code == 401
                print(json.dumps({'native_refresh_ttl_seconds': int(claims['exp'] - time.time()),
                                  'refreshes': 4, 'restart_recovery': True, 'expired_access_recovered': True,
                                  'mcp_tools_verified': True, 'revocation_verified': True,
                                  'live_government_verified': government_verified}))

        asyncio.run(run())
