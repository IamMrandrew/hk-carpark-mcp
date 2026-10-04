"""Synthetic crypto tests; NOT Andrew identity/passkey evidence."""
import asyncio
import importlib
import importlib.util
import json
import time
from dataclasses import FrozenInstanceError

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.synthetic_support import binding, identity_db, seed_grant

PUBLIC = 'https://carpark.laporatory.com'


def test_immutable_identity_crypto_and_live_invalidation(tmp_path):
    assert importlib.util.find_spec('hkparking.auth.integration') is not None, 'production integration missing'
    m = importlib.import_module('hkparking.auth.integration')
    path=identity_db(tmp_path/'identity.db')
    cfg = m.PocketConfig(allowed_sub='fixture-sub', client_id='fixture-client', loopback_port=1411,identity_database=str(path),native_release=m.RELEASE,identity_file=binding(path))
    try:
        cfg.allowed_sub = 'other'
        assert False, 'subject policy must be immutable'
    except FrozenInstanceError:
        pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='fixture-key', alg='RS256', use='sig')
    state = {'active': True, 'disabled': False, 'calls': []}
    def response(request):
        state['calls'].append(request.url.path)
        if request.url.path == '/.well-known/jwks.json':
            return httpx.Response(200, json={'keys': [jwk]})
        if request.url.path == '/api/oidc/introspect':
            return httpx.Response(200, json={'active': state['active']})
        return httpx.Response(401 if state['disabled'] else 200, json={'sub': cfg.allowed_sub})
    transport = httpx.MockTransport(response)
    verifier = m.PocketVerifier(cfg, 'fixture-secret', transport=transport)
    base = {'iss':PUBLIC, 'aud':[PUBLIC, cfg.client_id], 'sub':cfg.allowed_sub,
                'client_id':cfg.client_id, 'exp':int(time.time())+60, 'iat':int(time.time()),
                'jti':'fixture-jti', 'scope':'openid'}
    def token(changes=None, typ='at+jwt', signing_key=key):
        claims={**base,**(changes or {})}
        result=jwt.encode(claims, signing_key, algorithm='RS256',
                          headers={'kid': 'fixture-key', 'typ': typ})
        seed_grant(path,result,claims)
        return result
    async def run():
        result = await verifier.verify_token(token())
        assert result is not None
        assert result.scopes == ['parking:read']
        assert result.resource == PUBLIC+'/mcp'
        for change in [{'sub':'other'}, {'iss':'https://evil.invalid'}, {'client_id':'other'},
                       {'aud':['other']}, {'scope':'profile'}, {'scope':'openid extra'},
                       {'exp':int(time.time())-60}, {'exp':None}, {'client_id':None}]:
            assert await verifier.verify_token(token(change)) is None, change
        assert await verifier.verify_token(token(typ='JWT')) is None
        assert await verifier.verify_token(token(signing_key=rsa.generate_private_key(public_exponent=65537,key_size=2048))) is None
        state['active'] = False
        assert await verifier.verify_token(token()) is None
        state['active'] = True
        state['disabled'] = True
        assert await verifier.verify_token(token()) is None
        await verifier.aclose()
    asyncio.run(run())
