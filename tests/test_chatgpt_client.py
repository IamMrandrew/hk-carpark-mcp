"""Real SDK DCR/authentication boundary tests; only synthetic credentials."""
import asyncio

import httpx
import pytest
from mcp.shared.auth import InvalidRedirectUriError
from pydantic import AnyUrl

from hkparking.auth import integration as m
from tests.synthetic_support import binding, identity_db

CHATGPT = 'https://chatgpt.com/connector_platform_oauth_redirect'
CALLBACK = 'https://chatgpt.com/connector/oauth/SYNTHETIC-callback-123'


def make_app(tmp_path):
    path = identity_db(tmp_path/'identity.db')
    cfg = m.PocketConfig('fixture-sub','fixture-client',1411,str(path),m.RELEASE,binding(path))
    return m.build_integration(tmp_path/'bridge',cfg,'SYNTHETIC-secret',
                              pocket_transport=httpx.MockTransport(lambda r:httpx.Response(404)))


def metadata(callback, method='none'):
    return {'redirect_uris':[callback], 'token_endpoint_auth_method':method,
            'grant_types':['refresh_token','authorization_code']}


@pytest.mark.parametrize('callback', [CHATGPT, CALLBACK, 'https://arbitrary.example.org/oauth/callback?tenant=a'])
def test_chatgpt_registration_authorize_and_exact_client_binding(tmp_path, callback):
    app = make_app(tmp_path)
    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app),base_url=m.PUBLIC
        ) as client:
            response = await client.post('/register',json={**metadata(callback),
                'client_uri':'https://example.org/', 'logo_uri':'https://example.org/logo.png',
                'contacts':['support@example.org'], 'software_id':'synthetic','software_version':'1'})
            assert response.status_code == 201, response.text
            identifier = response.json()['client_id']
            registered = await app.state.provider.get_client(identifier)
            assert str(registered.validate_redirect_uri(AnyUrl(callback))) == callback
            other = 'https://different.example.org/callback'
            with pytest.raises(InvalidRedirectUriError):
                registered.validate_redirect_uri(AnyUrl(other))
            with pytest.raises(InvalidRedirectUriError):
                registered.validate_redirect_uri(AnyUrl(callback + ('&' if '?' in callback else '?') + 'substitute=1'))
            params={'client_id':identifier,'redirect_uri':callback,'response_type':'code',
                    'scope':'parking:read','resource':m.PUBLIC+'/mcp','state':'SYNTHETIC-state',
                    'code_challenge':'x'*43,'code_challenge_method':'S256'}
            assert (await client.get('/bridge/authorize',params=params)).status_code == 302
            for changes in [{'redirect_uri':other},{'client_id':'unregistered'},
                            {'code_challenge_method':'plain'},{'scope':'admin'}, {'resource':'https://other.example/mcp'}]:
                assert (await client.get('/bridge/authorize',params={**params,**changes})).status_code >= 400
    asyncio.run(run())


@pytest.mark.parametrize('method', ['client_secret_post', 'client_secret_basic', None])
def test_confidential_registration_keeps_secret_and_sdk_verification(tmp_path, method):
    app = make_app(tmp_path)
    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app),base_url=m.PUBLIC
        ) as client:
            body = metadata('https://arbitrary.example.org/callback', method)
            if method is None:
                del body['token_endpoint_auth_method']
            response = await client.post('/register',json=body)
            assert response.status_code == 201, response.text
            info = response.json()
            expected = method or 'client_secret_basic'
            stored = await app.state.provider.get_client(info['client_id'])
            assert info['token_endpoint_auth_method'] == stored.token_endpoint_auth_method == expected
            assert info['client_secret'] == stored.client_secret
            data={'grant_type':'refresh_token','refresh_token':'nonexistent', 'resource':m.PUBLIC+'/mcp'}
            if expected == 'client_secret_post':
                data['client_id'] = info['client_id']
                valid = {'data':{**data,'client_secret':info['client_secret']}}
                wrong = {'data':{**data,'client_secret':'wrong'}}
            else:
                valid = {'data':data,'auth':(info['client_id'], info['client_secret'])}
                wrong = {'data':data,'auth':(info['client_id'], 'wrong')}
            # Correct credentials reach grant validation, wrong/missing do not.
            assert (await client.post('/token',**valid)).json()['error'] == 'invalid_grant'
            assert (await client.post('/token',**wrong)).json()['error'] == 'invalid_client'
            assert (await client.post('/token',data={**data,'client_id':info['client_id']})).json()['error'] == 'invalid_client'
    asyncio.run(run())
