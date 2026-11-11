"""Public discovery must describe one downstream OAuth server, never Pocket OIDC."""
import asyncio

import httpx

from hkparking.auth import integration as m
from tests.synthetic_support import binding, identity_db


def test_oauth_and_oidc_discovery_describe_same_bridge(tmp_path):
    database = identity_db(tmp_path/'identity.db')
    cfg = m.PocketConfig('fixture-sub','fixture-client',1411,str(database),m.RELEASE,binding(database))
    app = m.build_integration(tmp_path/'bridge',cfg,'SYNTHETIC-secret',
        pocket_transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
            'issuer':'SYNTHETIC-native-issuer','scopes_supported':['openid','profile','email','groups'],
            'userinfo_endpoint':m.PUBLIC+'/api/oidc/userinfo'})))
    async def run():
        async with app.router.lifespan_context(app),httpx.AsyncClient(
            transport=httpx.ASGITransport(app),base_url=m.PUBLIC
        ) as client:
            oauth = await client.get('/.well-known/oauth-authorization-server')
            oidc_alias = await client.get('/.well-known/openid-configuration')
            resource = (await client.get('/.well-known/oauth-protected-resource/mcp')).json()
            assert oauth.status_code == oidc_alias.status_code == 200
            assert oauth.json() == oidc_alias.json()
            metadata = oauth.json()
            assert metadata['issuer'] in resource['authorization_servers']
            assert metadata['authorization_endpoint'] == m.PUBLIC+'/bridge/authorize'
            assert metadata['scopes_supported'] == ['parking:read']
            assert set(metadata['token_endpoint_auth_methods_supported']) == {'none','client_secret_post','client_secret_basic'}
            assert 'userinfo_endpoint' not in metadata
            assert 'jwks_uri' not in metadata
            assert not metadata.get('client_id_metadata_document_supported')
            assert not metadata.get('authorization_response_iss_parameter_supported')
            assert 'no-store' in oidc_alias.headers['cache-control']
    asyncio.run(run())
