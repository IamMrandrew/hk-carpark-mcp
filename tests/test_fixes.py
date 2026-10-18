"""Denial regressions adapted from independent review; SYNTHETIC only."""
import asyncio
import json
import sqlite3
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hkparking.auth import integration as m
from tests.synthetic_support import binding, identity_db, seed_grant


def test_fixed_dependency_factory_composes(tmp_path):
    cfg = m.PocketConfig('SYNTHETIC-sub','SYNTHETIC-client',1411,str(tmp_path/'identity.db'))
    app = m.build_integration(tmp_path/'bridge',cfg,'SYNTHETIC-secret',pocket_transport=httpx.MockTransport(lambda r:httpx.Response(200,json={})))
    async def close():
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.provider,m.PocketBridge)
    asyncio.run(close())


def test_exercise_is_import_safe_and_fresh():
    import importlib.util
    if importlib.util.find_spec('exercise') is None:
        pytest.skip('native exercise harness (pinned binary) not present in this checkout')
    import exercise
    assert callable(exercise.NativeFixture)
    with exercise.NativeFixture() as fixture:
        assert fixture.process.poll() is None
        assert exercise.owns_listener(fixture.process,fixture.port)
        assert fixture.home.stat().st_mode & 0o777 == 0o700
        assert fixture.database.exists()
        assert not (fixture.home/'runtime.json').exists()
    assert fixture.process.poll() is not None
    assert not fixture.home.exists()


def crypto_setup(tmp_path):
    path=identity_db(tmp_path/'identity.db','SYNTHETIC-sub','SYNTHETIC-client')
    cfg=m.PocketConfig('SYNTHETIC-sub','SYNTHETIC-client',1411,str(path),m.RELEASE,binding(path))
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='SYNTHETIC-key',alg='RS256',use='sig')
    state={'active':True,'calls':[],'delay':0}
    async def mock(request):
        state['calls'].append(request.url.path)
        if request.url.path.endswith('jwks.json'):
            return httpx.Response(200,json={'keys':[jwk]})
        if request.url.path.endswith('introspect'):
            return httpx.Response(200,json={'active':state['active']})
        if state['delay']:
            await asyncio.sleep(state['delay'])
        return httpx.Response(200,json={'sub':cfg.allowed_sub})
    v=m.PocketVerifier(cfg,'SYNTHETIC-secret',transport=httpx.MockTransport(mock))
    def token(changes=None):
        now=int(time.time())
        claims={'iss':m.PUBLIC,'aud':[m.PUBLIC,cfg.client_id],'sub':cfg.allowed_sub,'client_id':cfg.client_id,'scope':'openid','iat':now,'exp':now+300,'jti':'SYNTHETIC-jti'}
        claims={**claims,**(changes or {})}
        result=jwt.encode(claims,key,algorithm='RS256',headers={'kid':'SYNTHETIC-key','typ':'at+jwt'})
        seed_grant(path,result,claims)
        return result
    return v,token,state,path


def test_schema_view_denied(tmp_path):
    v,token,_state,path=crypto_setup(tmp_path)
    async def run():
        assert await v.verify_token(token()) is not None
        with sqlite3.connect(path) as db:
            db.execute('ALTER TABLE users RENAME TO actual_users')
            db.execute('UPDATE actual_users SET disabled=1')
            db.execute('CREATE VIEW users AS SELECT id,0 AS disabled FROM actual_users')
        assert await v.verify_token(token()) is None
        await v.aclose()
    asyncio.run(run())


def test_signed_expiry_after_await_denied(tmp_path):
    v,token,state,_path=crypto_setup(tmp_path)
    async def run():
        state['delay']=1.4
        assert await v.verify_token(token({'exp':int(time.time())+1})) is None
        await v.aclose()
    asyncio.run(run())


def test_stored_expiry_after_await_denied(tmp_path):
    from fastmcp.server.auth import TokenVerifier
    from fastmcp.server.auth.oauth_proxy.models import UpstreamTokenSet
    from mcp.server.auth.provider import AccessToken

    from hkparking.auth.bridge import RESOURCE, SCOPE, make_provider
    class Delayed(TokenVerifier):
        async def verify_token(self,token):
            await asyncio.sleep(.2)
            return AccessToken(token=token,client_id='SYNTHETIC-native',scopes=SCOPE,resource=RESOURCE,expires_at=int(time.time())+300)
    p=make_provider(tmp_path,token_verifier=Delayed(),upstream_authorization_endpoint='http://127.0.0.1/authorize',upstream_token_endpoint='http://127.0.0.1/token',upstream_revocation_endpoint=None,upstream_client_id='SYNTHETIC-native',upstream_client_secret='SYNTHETIC-secret')
    async def run():
        t=p.jwt_issuer.issue_access_token(client_id='SYNTHETIC-downstream',scopes=SCOPE,jti='SYNTHETIC-jti',expires_in=300)
        claims=p.jwt_issuer.verify_token(t)
        p.ledger.record('SYNTHETIC-family','SYNTHETIC-downstream',[claims])
        u=UpstreamTokenSet(upstream_token_id='SYNTHETIC-family',access_token='SYNTHETIC-upstream',refresh_token=None,refresh_token_expires_at=None,expires_at=time.time()+.1,client_id='SYNTHETIC-downstream',created_at=time.time(),scope='parking:read',token_type='Bearer',raw_token_data={})
        await p._upstream_token_store.put(key=u.upstream_token_id,value=u,ttl=300)
        assert await p.load_access_token(t) is None
    try:
        asyncio.run(run())
    finally:
        p.disk._cache.close();p.ledger.db.close()


def downstream_client():
    from mcp.shared.auth import OAuthClientInformationFull
    return OAuthClientInformationFull(client_id='SYNTHETIC-downstream',redirect_uris=['https://grok.com/connectors-oauth-exchange-code/'],token_endpoint_auth_method='none',scope='parking:read',grant_types=['authorization_code','refresh_token'],response_types=['code'])


async def seed_code(p,client,token):
    from fastmcp.server.auth.oauth_proxy.models import ClientCode
    now=time.time()
    from tests.test_refresh_gate import seed_refresh_session
    cfg=p._token_validator.config
    seed_refresh_session(cfg.identity_database,cfg.allowed_sub,cfg.client_id,
                         'SYNTHETIC-upstream-refresh','SYNTHETIC-native-grant')
    code=ClientCode(code='SYNTHETIC-code',client_id=client.client_id,redirect_uri=str(client.redirect_uris[0]),code_challenge='x'*43,code_challenge_method='S256',scopes=['parking:read'],idp_tokens={'access_token':token,'refresh_token':'SYNTHETIC-upstream-refresh','scope':'openid','expires_in':300},expires_at=now+300,created_at=now)
    await p._code_store.put(key=code.code,value=code,ttl=300)
    return await p.load_authorization_code(client,code.code)


def test_wrong_owner_issuance_denied_without_live_grant(tmp_path):
    from mcp.server.auth.provider import TokenError
    v,token,_state,_path=crypto_setup(tmp_path)
    app=m.build_integration(tmp_path/'bridge',v.config,'SYNTHETIC-secret',pocket_transport=v.http._transport)
    p=app.state.provider;client=downstream_client()
    async def run():
        async with app.router.lifespan_context(app):
            await p.register_client(client)
            code=await seed_code(p,client,token({'sub':'SYNTHETIC-OTHER-OWNER'}))
            with pytest.raises(TokenError):
                await p.exchange_authorization_code(client,code)
            assert p.ledger.db.execute("SELECT count(*) FROM families WHERE status='live'").fetchone()[0]==0
            assert p.ledger.db.execute("SELECT exp IS NULL FROM admission WHERE id=?",(client.client_id,)).fetchone()[0]==0
        await v.aclose()
    asyncio.run(run())


def test_other_native_interaction_denied_all_entrypoints(tmp_path):
    path=identity_db(tmp_path/'identity.db','SYNTHETIC-sub','SYNTHETIC-client')
    cfg=m.PocketConfig('SYNTHETIC-sub','SYNTHETIC-client',1411,str(path),m.RELEASE,binding(path))
    iid='11111111-1111-1111-1111-111111111111';seen=[]
    def mock(r):
        seen.append(r.url.path)
        return httpx.Response(200,json={'id':iid,'client':{'id':'SYNTHETIC-OTHER-CLIENT'},'scopes':['profile']})
    app=m.build_integration(tmp_path/'bridge',cfg,'SYNTHETIC-secret',pocket_transport=httpx.MockTransport(mock))
    async def run():
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url=m.PUBLIC) as c:
            assert (await c.get('/api/oidc/interactions/'+iid)).status_code==400
            assert (await c.post('/api/oidc/interactions/'+iid+'/complete',json={'step':'consent'})).status_code==400
            assert (await c.get('/authorize',params={'interaction':iid})).status_code==400
            assert not any(p.endswith('/complete') for p in seen)
    asyncio.run(run())


def test_dynamic_protocol_log_redaction(tmp_path):
    import io
    import logging

    v,_token,_state,_path=crypto_setup(tmp_path)
    app=m.build_integration(tmp_path/'bridge',v.config,'SYNTHETIC-secret',pocket_transport=v.http._transport)
    output=io.StringIO();handler=logging.StreamHandler(output);root=logging.getLogger()
    root.addHandler(handler)
    try:
        logger=logging.getLogger('httpx.SYNTHETIC.future.logger')
        logger.setLevel(logging.ERROR)
        logger.error('SYNTHETIC-TOKEN-SENTINEL recovery /lc/SYNTHETIC-RECOVERY')
        assert 'SYNTHETIC-TOKEN-SENTINEL' not in output.getvalue()
        assert 'SYNTHETIC-RECOVERY' not in output.getvalue()
    finally:
        root.removeHandler(handler)
        async def close():
            async with app.router.lifespan_context(app):
                pass
            await v.aclose()
        asyncio.run(close())
