"""Validation and persisted-client regression tests for dynamic registration."""
import asyncio

import httpx
import pytest
from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from hkparking.auth import integration as m
from tests.test_chatgpt_client import make_app, metadata


@pytest.mark.parametrize('callback', [
    'http://example.org/callback', 'https://example.org/cb#fragment',
    'https://user:pass@example.org/cb', 'https://*.example.org/cb',
    'https://example.org/cb*', 'https://127.0.0.1/cb', 'https://192.168.1.1/cb',
    'https://localhost/cb', 'https://host.local/cb', 'https://example.org/cb\\evil',
])
def test_unsafe_callback_denied_at_http_and_direct_provider(tmp_path, callback):
    app = make_app(tmp_path)
    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app),base_url=m.PUBLIC
        ) as client:
            assert (await client.post('/register',json=metadata(callback))).status_code == 400
            info = OAuthClientInformationFull(client_id='direct-synthetic', **metadata(callback))
            # Pydantic normalizes backslashes before a provider receives AnyUrl.
            # Exercise the raw direct-call boundary too, without that normalization.
            if '\\' in callback:
                info = info.model_copy(update={'redirect_uris':[callback]})
            with pytest.raises(RegistrationError):
                await app.state.provider.register_client(info)
            assert await app.state.provider.get_client(info.client_id) is None
    asyncio.run(run())


@pytest.mark.parametrize('changes', [
    {'grant_types':['authorization_code']}, {'grant_types':[]}, {'response_types':['token']},
    {'token_endpoint_auth_method':'private_key_jwt'}, {'client_name':'x'*81},
    {'logo_uri':'https://example.org/'+'x'*2048}, {'contacts':['x']*5}, {'jwks':{'keys':[]}},
])
def test_unsupported_or_unbounded_metadata_is_rejected(tmp_path, changes):
    app = make_app(tmp_path)
    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app),base_url=m.PUBLIC
        ) as client:
            response = await client.post('/register',json={**metadata('https://example.org/cb'),**changes})
            assert response.status_code == 400
            assert response.json()['error'] == 'invalid_client_metadata'
            info = OAuthClientInformationFull(client_id='direct-bad',
                **{**metadata('https://example.org/cb'), **changes})
            with pytest.raises(RegistrationError):
                await app.state.provider.register_client(info)
    asyncio.run(run())


def test_legacy_public_client_preserved_and_direct_authorize_uses_stored_callbacks(tmp_path):
    app = make_app(tmp_path)
    async def run():
        async with app.router.lifespan_context(app):
            provider = app.state.provider
            identifier = 'synthetic-legacy-grok'
            provider.ledger.admit('registration',identifier,16,86400)
            legacy = ProxyDCRClient(client_id=identifier, token_endpoint_auth_method='none',
                                    redirect_uris=[AnyUrl('https://grok.com/connectors-oauth-exchange-code/')],
                                    scope='parking:read', allowed_redirect_uri_patterns=['https://*'])
            await provider._client_store.put(key=identifier,value=legacy)
            loaded = await provider.get_client(identifier)
            assert loaded.token_endpoint_auth_method == 'none' and loaded.client_secret is None
            # A direct caller must not substitute metadata even for a real ID.
            forged = loaded.model_copy(update={'redirect_uris':[AnyUrl('https://other.example.org/cb')]})
            params = AuthorizationParams(state='synthetic-state',scopes=['parking:read'],
                code_challenge='x'*43,redirect_uri=AnyUrl('https://other.example.org/cb'),
                redirect_uri_provided_explicitly=True,resource=m.PUBLIC+'/mcp')
            with pytest.raises(AuthorizeError):
                await provider.authorize(forged,params)
            assert await provider.get_client('https://unregistered.example.org/metadata') is None
    asyncio.run(run())
