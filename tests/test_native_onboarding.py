"""Real pinned Pocket; disposable accounts and software WebAuthn keys only."""
import asyncio
import base64
import hashlib
import json
import secrets
import struct
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from hkparking.auth.integration import PUBLIC, RELEASE, PocketConfig, build_integration
from tests.test_native_refresh_lifecycle import native_fixture


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def cbor(value):
    """Small fixture-only CBOR encoder for none attestation / EC2 COSE keys."""
    def head(major, number):
        if number < 24:
            return bytes([(major << 5) | number])
        if number < 256:
            return bytes([(major << 5) | 24, number])
        return bytes([(major << 5) | 25]) + struct.pack('>H', number)
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        data = value.encode()
        return head(3, len(data)) + data
    if isinstance(value, dict):
        return head(5, len(value)) + b''.join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError('Unsupported synthetic CBOR value')


def test_real_native_invite_first_passkey_and_replay(tmp_path):
    with native_fixture(tmp_path / 'native') as fixture:
        with httpx.Client(base_url=f'http://127.0.0.1:{fixture["port"]}',
                          headers={'host': urlsplit(PUBLIC).netloc, 'x-forwarded-proto': 'https'},
                          cookies=fixture['cookies'], trust_env=False, timeout=5) as admin:
            admin.headers['cookie'] = '; '.join(k + '=' + v for k, v in admin.cookies.items())
            response = admin.post('/api/users', json={
                'username': 'SYNTHETIC-invited', 'email': None,
                'emailVerified': False, 'firstName': 'Synthetic', 'lastName': '',
                'displayName': 'Synthetic invite', 'isAdmin': False, 'disabled': False,
                'userGroupIds': [],
            })
            assert response.status_code == 201, response.text
            user = response.json()
            assert user['isAdmin'] is False and user.get('email') is None
            invite = admin.post('/api/users/' + user['id'] + '/one-time-access-token', json={'ttl': '20m'})
            assert invite.status_code == 201
            code = invite.json()['token']
            assert len(code) == 12

        stat = fixture['database'].stat()
        cfg = PocketConfig('fixture-sub', fixture['client'], fixture['port'],
                           str(fixture['database']), RELEASE, (stat.st_dev, stat.st_ino))
        app = build_integration(tmp_path / 'bridge', cfg, fixture['secret'])
        import time
        app.state.enrollment.approve(code, user['id'], int(time.time()) + 1200)

        async def run():
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=PUBLIC, timeout=10,
                headers={'Origin': PUBLIC}
            ) as browser:
                assert (await browser.get('/lc/' + code)).status_code == 200
                assert (await browser.get('/login/alternative/code', params={'code': code})).status_code == 200
                response = await browser.post('/api/one-time-access-token/' + code)
                assert response.status_code == 200
                assert response.json()['id'] == user['id']
                me = await browser.get('/api/users/me')
                assert me.status_code == 200 and me.json()['id'] == user['id']
                assert (await browser.post('/api/one-time-access-token/' + code)).status_code >= 400
                assert (await browser.get('/signup/add-passkey')).status_code == 200
                options = await browser.get('/api/webauthn/register/start')
                assert options.status_code == 200
                options = options.json()
                assert options['rp']['id'] == urlsplit(PUBLIC).hostname
                private = ec.generate_private_key(ec.SECP256R1())
                public = private.public_key().public_numbers()
                cred_id = secrets.token_bytes(32)
                cose = cbor({1: 2, 3: -7, -1: 1,
                             -2: public.x.to_bytes(32, 'big'), -3: public.y.to_bytes(32, 'big')})
                rp_hash = hashlib.sha256(options['rp']['id'].encode()).digest()
                auth_data = rp_hash + b'\x45' + b'\0' * 4 + b'\0' * 16 + struct.pack('>H', len(cred_id)) + cred_id + cose
                client_data = json.dumps({'type': 'webauthn.create', 'challenge': options['challenge'],
                                          'origin': PUBLIC, 'crossOrigin': False}).encode()
                response = await browser.post('/api/webauthn/register/finish', json={
                    'id': b64(cred_id), 'rawId': b64(cred_id), 'type': 'public-key',
                    'authenticatorAttachment': 'platform', 'clientExtensionResults': {},
                    'response': {'clientDataJSON': b64(client_data),
                                 'attestationObject': b64(cbor({'fmt': 'none', 'authData': auth_data, 'attStmt': {}})),
                                 'transports': ['internal']},
                })
                assert response.status_code == 200
                credentials = await browser.get('/api/webauthn/credentials')
                assert credentials.status_code == 200 and len(credentials.json()) == 1
                assert (await browser.get('/api/webauthn/register/start')).status_code == 403
                assert (await browser.post('/api/webauthn/register/finish', json={})).status_code == 403
                assert (await browser.post('/api/webauthn/logout')).status_code in (200, 204)
                assert (await browser.get('/api/webauthn/register/start')).status_code == 403
                options = (await browser.get('/api/webauthn/login/start')).json()
                client_data = json.dumps({'type': 'webauthn.get', 'challenge': options['challenge'],
                                          'origin': PUBLIC, 'crossOrigin': False}).encode()
                auth_data = rp_hash + b'\x05' + struct.pack('>I', 1)
                signature = private.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
                response = await browser.post('/api/webauthn/login/finish', json={
                    'id': b64(cred_id), 'rawId': b64(cred_id), 'type': 'public-key',
                    'authenticatorAttachment': 'platform', 'clientExtensionResults': {},
                    'response': {'clientDataJSON': b64(client_data), 'authenticatorData': b64(auth_data),
                                 'signature': b64(signature), 'userHandle': b64(user['id'].encode())},
                })
                assert response.status_code == 200
                assert response.json()['id'] == user['id']
                assert (await browser.get('/mcp')).status_code == 401
        asyncio.run(run())
