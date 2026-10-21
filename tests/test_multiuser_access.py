"""Multi-subject authorization regressions; all identities and grants are SYNTHETIC."""
import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace

import httpx
import jwt
import pytest

from hkparking.auth import integration as m
from tests.synthetic_support import binding, identity_db
from tests.test_fixes import crypto_setup
from tests.test_refresh_gate import seed_refresh_session

OWNER = 'SYNTHETIC-sub'
SECOND = 'SYNTHETIC-second'
UNKNOWN = 'SYNTHETIC-unlisted'
CLIENT = 'SYNTHETIC-client'


def add_user(path, subject, *, disabled=False):
    with sqlite3.connect(path) as db:
        db.execute(
            'INSERT INTO users(id,username,first_name,last_name,display_name,disabled) '
            'VALUES(?,?,?,?,?,?)',
            (subject, subject, 'SYNTHETIC', '', 'SYNTHETIC', int(disabled)),
        )


def test_config_keeps_owner_and_explicit_subject_allowlist():
    cfg = m.PocketConfig(OWNER, CLIENT, 1411)
    assert cfg.additional_allowed_subs == ()
    assert cfg.allows_subject(OWNER)
    assert not cfg.allows_subject(SECOND)
    cfg = replace(cfg, additional_allowed_subs=(SECOND,))
    assert cfg.allowed_sub == OWNER
    assert cfg.allows_subject(OWNER)
    assert cfg.allows_subject(SECOND)
    assert not cfg.allows_subject(UNKNOWN)
    assert not cfg.allows_subject(None)


@pytest.mark.parametrize('subject,disabled,accepted', [
    (OWNER, False, True),
    (SECOND, False, True),
    (UNKNOWN, False, False),
    (SECOND, True, False),
])
def test_signed_access_token_checks_actual_subject(tmp_path, subject, disabled, accepted):
    original, token, _state, path = crypto_setup(tmp_path)
    if subject != OWNER:
        add_user(path, subject, disabled=disabled)
    cfg = replace(original.config, additional_allowed_subs=(SECOND,))

    async def mock(request):
        if request.url.path.endswith('/userinfo'):
            # This is the synthetic issuer response, not verification logic:
            # real signature verification still uses crypto_setup's RSA/JWKS.
            bearer = request.headers['authorization'].split(' ', 1)[1]
            claims = jwt.decode(bearer, options={'verify_signature': False})
            return httpx.Response(200, json={'sub': claims['sub']})
        return await original.http._transport.handle_async_request(request)

    verifier = m.PocketVerifier(cfg, 'SYNTHETIC-secret', transport=httpx.MockTransport(mock))

    async def run():
        try:
            result = await verifier.verify_token(token({'sub': subject}))
            assert (result is not None) is accepted
            if accepted:
                assert result.subject == subject
                assert result.client_id == CLIENT
                assert result.claims['native_grant'] == hashlib.sha256(b'SYNTHETIC-native-grant').hexdigest()
        finally:
            await verifier.aclose()
            await original.aclose()
    asyncio.run(run())


def test_refresh_binding_cannot_cross_allowed_subjects(tmp_path):
    path = identity_db(tmp_path / 'identity.db', OWNER, CLIENT)
    add_user(path, SECOND)
    cfg = m.PocketConfig(OWNER, CLIENT, 1411, str(path), m.RELEASE, binding(path),
                         additional_allowed_subs=(SECOND,))
    status = m.NativeStatus(cfg)
    # Deliberately share client and grant fingerprint: subject alone must bind.
    grant = 'SYNTHETIC-shared-grant'
    digest = hashlib.sha256(grant.encode()).hexdigest()
    seed_refresh_session(path, OWNER, CLIENT, 'SYNTHETIC-owner-sig', grant)
    seed_refresh_session(path, SECOND, CLIENT, 'SYNTHETIC-second-sig', grant)
    owner_expected = (OWNER, CLIENT, digest)
    second_expected = (SECOND, CLIENT, digest)
    assert status.refresh('part.SYNTHETIC-owner-sig', owner_expected) is True
    assert status.refresh('part.SYNTHETIC-second-sig', second_expected) is True
    assert status.refresh('part.SYNTHETIC-owner-sig', second_expected) is False
    assert status.refresh('part.SYNTHETIC-second-sig', owner_expected) is False
    with sqlite3.connect(path) as db:
        db.execute('UPDATE users SET disabled=1 WHERE id=?', (SECOND,))
    assert status.refresh('part.SYNTHETIC-second-sig', second_expected) is False
    assert status.refresh('part.SYNTHETIC-owner-sig', owner_expected) is True


@pytest.mark.parametrize('subject,expected_status', [(SECOND, 200), (UNKNOWN, 400)])
def test_interaction_entrypoints_enforce_subject_allowlist(tmp_path, subject, expected_status):
    path = identity_db(tmp_path / 'identity.db', OWNER, CLIENT)
    add_user(path, subject)
    cfg = m.PocketConfig(OWNER, CLIENT, 1411, str(path), m.RELEASE, binding(path),
                         additional_allowed_subs=(SECOND,))
    iid = '11111111-1111-1111-1111-111111111111'
    params = {'client_id': CLIENT, 'redirect_uri': m.PUBLIC + '/auth/callback',
              'response_type': 'code', 'scope': 'openid', 'state': 'SYNTHETIC-state',
              'code_challenge': 'x' * 43, 'code_challenge_method': 'S256'}
    with sqlite3.connect(path) as db:
        db.execute(
            'INSERT INTO interaction_sessions(id,created_at,requested_at,client_id,scopes,user_id,parameters) '
            'VALUES(?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,?,?,?)',
            (iid, CLIENT, json.dumps(['openid']), subject, json.dumps(params)),
        )
    seen = []

    def mock(request):
        seen.append((request.method, request.url.path))
        if request.url.path == '/api/users/me':
            return httpx.Response(200, json={'id': subject})
        return httpx.Response(200, json={'id': iid, 'client': {'id': CLIENT}, 'scopes': ['openid']})

    app = m.build_integration(tmp_path / 'bridge', cfg, 'SYNTHETIC-secret',
                              pocket_transport=httpx.MockTransport(mock))

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=m.PUBLIC,
        ) as client:
            assert (await client.get('/api/oidc/interactions/' + iid)).status_code == expected_status
            assert (await client.post('/api/oidc/interactions/' + iid + '/complete',
                                      json={'step': 'consent'})).status_code == expected_status
            assert (await client.get('/authorize', params={'interaction': iid})).status_code == expected_status
            assert (('POST', '/api/oidc/interactions/' + iid + '/complete') in seen) is (expected_status == 200)
    asyncio.run(run())
