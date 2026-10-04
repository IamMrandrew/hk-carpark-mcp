"""SYNTHETIC payload-safe callback/error regressions, real SDK code path."""
import asyncio
import time

import httpx
from fastmcp.server.auth.oauth_proxy.models import OAuthTransaction
from starlette.requests import Request
from starlette.responses import Response

from hkparking.auth import integration as m
from tests.test_fixes import crypto_setup


def test_callback_error_query_payload_denied(tmp_path):
    v,_token,_state,_path=crypto_setup(tmp_path)
    app=m.build_integration(tmp_path/'bridge',v.config,'SYNTHETIC-secret',pocket_transport=v.http._transport)
    async def run():
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url=m.PUBLIC) as c:
            r=await c.get('/auth/callback',params={'error':'SYNTHETIC-ERROR-SENTINEL','error_description':'SYNTHETIC-TOKEN-SENTINEL'})
            assert r.status_code==400
            assert 'SYNTHETIC-' not in r.text
            assert 'SYNTHETIC-' not in r.headers.get('location','')
        await v.aclose()
    asyncio.run(run())


def test_callback_upstream_exception_payload_denied(tmp_path):
    v,_token,_state,_path=crypto_setup(tmp_path)
    def fail(request):
        return httpx.Response(400,json={'error':'invalid_grant','error_description':'SYNTHETIC-UPSTREAM-PAYLOAD-SENTINEL'})
    app=m.build_integration(tmp_path/'bridge',v.config,'SYNTHETIC-secret',pocket_transport=httpx.MockTransport(fail))
    p=app.state.provider
    async def run():
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url=m.PUBLIC) as c:
            txn=OAuthTransaction(txn_id='SYNTHETIC-txn',client_id='SYNTHETIC-client',client_redirect_uri='https://grok.com/connectors-oauth-exchange-code/',client_state='SYNTHETIC-state',code_challenge='x'*43,code_challenge_method='S256',scopes=['parking:read'],created_at=time.time(),proxy_code_verifier='x'*64,consent_token='SYNTHETIC-binding')
            await p._transaction_store.put(key=txn.txn_id,value=txn,ttl=300)
            response=Response()
            request=Request({'type':'http','method':'GET','scheme':'https','path':'/','headers':[],'query_string':b'','server':('carpark.laporatory.com',443)})
            p._set_consent_binding_cookie(request,response,txn.txn_id,txn.consent_token)
            cookie=response.headers['set-cookie'].split(';',1)[0]
            r=await c.get('/auth/callback',params={'state':txn.txn_id,'code':'SYNTHETIC-code'},headers={'cookie':cookie})
            assert r.status_code==500
            assert 'SYNTHETIC-UPSTREAM-PAYLOAD-SENTINEL' not in r.text
        await v.aclose()
    asyncio.run(run())
