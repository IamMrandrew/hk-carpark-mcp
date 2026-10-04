import asyncio
import importlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx

PUBLIC = 'https://carpark.laporatory.com'
from tests.synthetic_support import binding, identity_db


def test_composition_discovery_routes_boundary_and_registration(tmp_path):
    m = importlib.import_module('hkparking.auth.integration')
    assert hasattr(m, 'build_integration'), 'composition missing'
    path=identity_db(tmp_path/'identity.db')
    cfg = m.PocketConfig(allowed_sub='fixture-sub',client_id='fixture-client',loopback_port=1411,identity_database=str(path),native_release=m.RELEASE,identity_file=binding(path))
    seen=[]
    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == '/.well-known/openid-configuration':
            return httpx.Response(200,json={'issuer':PUBLIC,'authorization_endpoint':PUBLIC+'/authorize'})
        return httpx.Response(200,json={'owner':'pocket','path':request.url.path})
    app = m.build_integration(tmp_path,cfg,'fixture-secret',pocket_transport=httpx.MockTransport(upstream))
    provider = app.state.provider
    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=PUBLIC) as c:
            discovery = (await c.get('/.well-known/oauth-authorization-server')).json()
            assert discovery['authorization_endpoint']==PUBLIC+'/bridge/authorize'
            assert discovery['revocation_endpoint']==PUBLIC+'/revoke'
            assert discovery['scopes_supported']==['parking:read']
            assert not discovery.get('client_id_metadata_document_supported',False)
            assert (await c.get('/.well-known/openid-configuration')).json()['authorization_endpoint']==PUBLIC+'/authorize'
            assert (await c.get('/')).json()['owner']=='pocket'
            assert (await c.get('/api/webauthn/login/start')).status_code==200
            for path in ['/setup','/setup/','/signup','/lc/a','/settings/admin','/api/users','/api/users/initial','/api/oidc/clients','/api/one-time-access-token','/api/oidc/par','/api/oidc/device/authorize','/api/unknown','/api/webauthn/register/start']:
                assert (await c.get(path)).status_code==404,path
            assert (await c.get('/authorize',params={'client_id':'attacker'})).status_code==400
            assert (await c.get('/authorize',params={'client_id':cfg.client_id,'scope':'parking:read','code_challenge_method':'plain'})).status_code==400
            q={'client_id':cfg.client_id,'redirect_uri':PUBLIC+'/auth/callback','response_type':'code','scope':'openid','state':'fixture','code_challenge':'x'*43,'code_challenge_method':'S256'}
            assert (await c.get('/authorize',params=q)).status_code==200
            assert (await c.get('/authorize',params=list(q.items())+[('client_id',cfg.client_id)])).status_code==400
            metadata={'redirect_uris':['https://grok.com/connectors-oauth-exchange-code/'],'token_endpoint_auth_method':'none','scope':'parking:read','grant_types':['authorization_code','refresh_token'],'response_types':['code']}
            for body in ['null','[]','[1]','"str"','{','{"scope":null}',json.dumps(metadata)[:-1]+',"scope":"parking:read"}']:
                assert (await c.post('/register',content=body,headers={'content-type':'application/json'})).status_code==400,body
            assert (await c.post('/register',json=metadata)).status_code==201
            count=provider.ledger.db.execute('select count(*) from admission').fetchone()[0]
            assert (await c.post('/register',json=metadata,headers={'Host':'evil.invalid'})).status_code==421
            assert (await c.post('/register',json=metadata,headers={'Origin':'https://evil.invalid'})).status_code==403
            assert provider.ledger.db.execute('select count(*) from admission').fetchone()[0]==count
            assert (await c.get('/mcp')).status_code==401
            resource=(await c.get('/.well-known/oauth-protected-resource/mcp')).json()
            assert resource['resource']==PUBLIC+'/mcp'
            assert resource['authorization_servers']==[PUBLIC+'/'] or resource['authorization_servers']==[PUBLIC]
            url=provider._build_upstream_authorize_url('fixture',{'scopes':['parking:read'],'proxy_code_verifier':'x'*64,'resource':PUBLIC+'/mcp'})
            assert parse_qs(urlsplit(url).query)['scope']==['openid']
            assert 'resource' not in parse_qs(urlsplit(url).query)
            assert provider._prepare_scopes_for_token_exchange(['parking:read'])==['openid']
            assert provider._prepare_scopes_for_upstream_refresh(['parking:read'])==['openid']
            assert provider._translate_scopes_from_idp(['openid'])==['parking:read']
            try:
                provider._translate_scopes_from_idp(['profile'])
                assert False,'missing identity scope must not become parking'
            except ValueError:
                pass
    asyncio.run(run())
