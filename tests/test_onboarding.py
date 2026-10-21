"""Synthetic invitation routing; no production users, tokens or passkeys."""
import asyncio
import importlib.util
import sqlite3
import time

import httpx
import pytest

from hkparking.auth import integration as m
from tests.test_fixes import crypto_setup


def test_invitation_store_binds_user_session_expiry_and_single_finish(tmp_path):
    assert importlib.util.find_spec('hkparking.auth.enrollment') is not None, 'invitation gate missing'
    from hkparking.auth.enrollment import EnrollmentStore
    store = EnrollmentStore(tmp_path / 'invitations.sqlite')
    code = 'Abcdef234567'
    store.approve(code, 'SYNTHETIC-invited-user', int(time.time()) + 1200)
    assert store.pending(code) == 'SYNTHETIC-invited-user'
    assert store.pending('WrongCode234') is None
    assert not store.bind(code, 'OTHER-user', 'SYNTHETIC-session')
    assert store.bind(code, 'SYNTHETIC-invited-user', 'SYNTHETIC-session')
    assert store.pending(code) is None
    assert store.subject('OTHER-session') is None
    assert store.subject('SYNTHETIC-session') == 'SYNTHETIC-invited-user'
    assert store.reserve_finish('SYNTHETIC-session')
    # A second worker opening the same DB cannot reserve the same grant.
    other = EnrollmentStore(tmp_path / 'invitations.sqlite')
    assert not other.reserve_finish('SYNTHETIC-session')
    store.close_grant('SYNTHETIC-session')
    assert store.subject('SYNTHETIC-session') is None
    store.approve('Abcdef234568', 'SYNTHETIC-invited-user', int(time.time()) - 1)
    assert store.pending('Abcdef234568') is None
    other.close()
    store.close()


def test_extended_invitation_requires_explicit_private_operator_bound(tmp_path):
    from hkparking.auth.enrollment import EnrollmentStore
    store = EnrollmentStore(tmp_path / 'invitations.sqlite')
    expiry = int(time.time()) + 3600
    with pytest.raises(ValueError):
        store.approve('Abcdef234567', 'SYNTHETIC-invited-user', expiry)
    store.approve('Abcdef234567', 'SYNTHETIC-invited-user', expiry, max_validity_seconds=86400)
    assert store.pending('Abcdef234567') == 'SYNTHETIC-invited-user'
    with pytest.raises(ValueError):
        store.approve('Abcdef234568', 'SYNTHETIC-invited-user', int(time.time()) + 86410,
                      max_validity_seconds=86400)
    for bound in [86401, True, 0, '86400']:
        with pytest.raises(ValueError):
            store.approve('Abcdef234568', 'SYNTHETIC-invited-user', expiry,
                          max_validity_seconds=bound)
    store.close()


def test_unapproved_code_is_denied_before_native_exchange(tmp_path):
    verifier, _token, _state, _path = crypto_setup(tmp_path)
    seen = []

    def native(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={'synthetic': True})

    app = m.build_integration(tmp_path / 'bridge', verifier.config, 'SYNTHETIC-secret',
                              pocket_transport=httpx.MockTransport(native))

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=m.PUBLIC,
            headers={'Origin': m.PUBLIC}
        ) as client:
            assert (await client.post('/api/one-time-access-token/Abcdef234567')).status_code == 403
            assert not seen
        await verifier.aclose()
    asyncio.run(run())


def test_admin_invite_pages_and_exchange_are_forwarded(tmp_path):
    verifier, _token, _state, _path = crypto_setup(tmp_path)
    seen = []

    def native(request):
        seen.append((request.method, request.url.path))
        if request.url.path.startswith('/api/one-time-access-token/'):
            return httpx.Response(200, json={'id': verifier.config.allowed_sub},
                                  headers={'Set-Cookie': '__Host-access_token=SYNTHETIC-session; Secure; HttpOnly; Path=/'})
        return httpx.Response(200, json={'synthetic': True})

    app = m.build_integration(tmp_path / 'bridge', verifier.config, 'SYNTHETIC-secret',
                              pocket_transport=httpx.MockTransport(native))

    app.state.enrollment.approve('Abcdef234567', verifier.config.allowed_sub, int(time.time()) + 1200)

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=m.PUBLIC, headers={'Origin': m.PUBLIC}
        ) as client:
            for path in ['/lc/Abcdef234567', '/login/alternative',
                         '/login/alternative/code', '/signup/add-passkey']:
                response = await client.get(path)
                assert response.status_code == 200, path
                assert ('GET', path) in seen
            exchange_path = '/api/one-time-access-token/Abcdef234567'
            assert (await client.post(exchange_path)).status_code == 200
            assert ('POST', exchange_path) in seen
            for method, path in [('GET', exchange_path), ('POST', '/lc/Abcdef234567'),
                                 ('GET', '/lc/a'), ('GET', '/signup'),
                                 ('POST', '/api/users'),
                                 ('POST', '/api/one-time-access-token'),
                                 ('POST', '/api/one-time-access-email')]:
                before = len(seen)
                assert (await client.request(method, path)).status_code == 404
                assert len(seen) == before
        await verifier.aclose()

    asyncio.run(run())


def test_invitation_session_never_leaks_to_another_browser(tmp_path):
    verifier, _token, _state, _path = crypto_setup(tmp_path)

    def native(request):
        if request.url.path.startswith('/api/one-time-access-token/'):
            return httpx.Response(200, json={'id': verifier.config.allowed_sub}, headers={
                'Set-Cookie': '__Host-access_token=SYNTHETIC-session; Secure; HttpOnly; Path=/'})
        cookie = request.headers.get('cookie', '')
        return httpx.Response(200 if cookie == '__Host-access_token=SYNTHETIC-session' else 401)

    app = m.build_integration(tmp_path / 'bridge', verifier.config, 'SYNTHETIC-secret',
                              pocket_transport=httpx.MockTransport(native))
    app.state.enrollment.approve('Abcdef234567', verifier.config.allowed_sub, int(time.time()) + 1200)

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=m.PUBLIC, headers={'Origin': m.PUBLIC}
        ) as invited, httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=m.PUBLIC
        ) as anonymous:
            assert (await invited.post('/api/one-time-access-token/Abcdef234567')).status_code == 200
            for _ in range(3):
                own, other = await asyncio.gather(invited.get('/api/users/me'), anonymous.get('/api/users/me'))
                assert own.status_code == 200
                assert other.status_code == 401
        await verifier.aclose()
    asyncio.run(run())


def test_first_passkey_requires_enabled_non_admin_without_credentials(tmp_path):
    verifier, _token, _state, path = crypto_setup(tmp_path)
    invited = 'SYNTHETIC-invited-user'
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO users(id,username,first_name,last_name,display_name,is_admin,disabled) '
                   'VALUES(?,?,?,?,?,0,0)', (invited, invited, 'Synthetic', '', 'Synthetic'))
    state = {'authenticated': True, 'user': invited}
    forwarded = []

    def native(request):
        if request.url.path == '/api/users/me':
            if not state['authenticated'] or request.headers.get('cookie') != '__Host-access_token=SYNTHETIC-session':
                return httpx.Response(401)
            return httpx.Response(200, json={'id': state['user']})
        forwarded.append(request.url.path)
        return httpx.Response(200, json={'synthetic': True})

    app = m.build_integration(tmp_path / 'bridge', verifier.config, 'SYNTHETIC-secret',
                              pocket_transport=httpx.MockTransport(native))

    app.state.enrollment.approve('Abcdef234567', invited, int(time.time()) + 1200)
    assert app.state.enrollment.bind('Abcdef234567', invited, 'SYNTHETIC-session')

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=m.PUBLIC,
            headers={'cookie': '__Host-access_token=SYNTHETIC-session', 'Origin': m.PUBLIC}
        ) as client:
            route = '/api/webauthn/register/start'
            assert (await client.get(route)).status_code == 200
            assert route in forwarded
            state['authenticated'] = False
            assert (await client.get(route)).status_code == 403
            state['authenticated'] = True
            for column in ['disabled', 'is_admin']:
                with sqlite3.connect(path) as db:
                    db.execute(f'UPDATE users SET {column}=1 WHERE id=?', (invited,))
                assert (await client.get(route)).status_code == 403
                with sqlite3.connect(path) as db:
                    db.execute(f'UPDATE users SET {column}=0 WHERE id=?', (invited,))
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO webauthn_credentials'
                           '(id,created_at,name,credential_id,public_key,attestation_type,transport,user_id) '
                           'VALUES(?,CURRENT_TIMESTAMP,?,?,?,?,?,?)',
                           ('SYNTHETIC-credential', 'Synthetic', b'credential', b'key', 'none', b'[]', invited))
            before = len(forwarded)
            assert (await client.get(route)).status_code == 403
            assert (await client.post('/api/webauthn/register/finish', json={})).status_code == 403
            assert len(forwarded) == before
            with sqlite3.connect(path) as db:
                db.execute('DELETE FROM webauthn_credentials WHERE user_id=?', (invited,))
            assert (await client.post('/api/webauthn/register/finish', json={})).status_code == 200
            assert (await client.get(route)).status_code == 403
            assert (await client.post(route)).status_code == 404
            assert (await client.get('/api/webauthn/register/finish')).status_code == 404
            # The onboarding change must not authorize another subject for MCP.
            assert (await client.get('/mcp')).status_code == 401
        await verifier.aclose()

    asyncio.run(run())
