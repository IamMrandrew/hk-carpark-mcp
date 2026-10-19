"""RED regression: pinned v2.17.0 stores refresh expires_at as INTEGER epoch.

The pre-refresh gate's NativeStatus.refresh must recognize a live refresh
session written the way the actual publisher-verified native binary writes it
(expires_at INTEGER Unix epoch seconds). unixepoch() on a bare INTEGER treats
it as a Julian day number and returns NULL, silently denying every real grant.
"""
import asyncio
import json
import time

from hkparking.auth import integration as m
from tests.synthetic_support import binding, identity_db


def seed_refresh_session(path, sub, client, signature, grant, *, expires_in=3600):
    data = {'id': grant, 'client_id': client, 'granted_scope': ['openid'],
            'session': {'subject': sub, 'jwt_claims': {'JTI': 'SYNTHETIC-jti'}}}
    with __import__('sqlite3').connect(path) as db:
        db.execute(
            "INSERT INTO oauth2_sessions(id,created_at,kind,key,request_id,client_id,"
            "access_token_signature,active,request_data,expires_at) "
            "VALUES(?,CURRENT_TIMESTAMP,'refresh_token',?,?,?,'',1,?,?)",
            ('SYNTHETIC-refresh-session-' + grant + '-' + signature, signature, grant, client,
             json.dumps(data), int(time.time()) + expires_in))


def test_refresh_gate_accepts_integer_epoch_expiry(tmp_path):
    path = identity_db(tmp_path / 'identity.db', 'SYNTHETIC-sub', 'SYNTHETIC-client')
    signature = 'SYNTHETIC-refresh-signature'
    seed_refresh_session(path, 'SYNTHETIC-sub', 'SYNTHETIC-client', signature, 'SYNTHETIC-native-grant')
    cfg = m.PocketConfig('SYNTHETIC-sub', 'SYNTHETIC-client', 1411, str(path), m.RELEASE, binding(path))
    status = m.NativeStatus(cfg)
    expected = ('SYNTHETIC-sub', 'SYNTHETIC-client',
                __import__('hashlib').sha256(b'SYNTHETIC-native-grant').hexdigest())
    token = 'part.' + signature

    async def run():
        assert status.refresh(token, expected) is True, 'live INTEGER-epoch refresh session denied'

    asyncio.run(run())


def test_refresh_gate_denies_expired_integer_epoch(tmp_path):
    path = identity_db(tmp_path / 'identity.db', 'SYNTHETIC-sub', 'SYNTHETIC-client')
    signature = 'SYNTHETIC-refresh-signature'
    seed_refresh_session(path, 'SYNTHETIC-sub', 'SYNTHETIC-client', signature, 'SYNTHETIC-native-grant',
                         expires_in=-60)
    cfg = m.PocketConfig('SYNTHETIC-sub', 'SYNTHETIC-client', 1411, str(path), m.RELEASE, binding(path))
    status = m.NativeStatus(cfg)
    expected = ('SYNTHETIC-sub', 'SYNTHETIC-client',
                __import__('hashlib').sha256(b'SYNTHETIC-native-grant').hexdigest())

    async def run():
        assert status.refresh('part.' + signature, expected) is False

    asyncio.run(run())
