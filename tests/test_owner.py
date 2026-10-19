"""SYNTHETIC issuance/refresh provenance, no production authority."""
import asyncio

import pytest
from mcp.server.auth.provider import TokenError

from hkparking.auth import integration as m
from tests.test_fixes import crypto_setup, downstream_client, seed_code


def test_native_inactive_refresh_denied_before_exchange(tmp_path):
    v,token,state,_path=crypto_setup(tmp_path)
    app=m.build_integration(tmp_path/'bridge',v.config,'SYNTHETIC-secret',pocket_transport=v.http._transport)
    p=app.state.provider;client=downstream_client()
    async def run():
        async with app.router.lifespan_context(app):
            await p.register_client(client)
            result=await p.exchange_authorization_code(client,await seed_code(p,client,token()))
            refresh=await p.load_refresh_token(client,result.refresh_token)
            state['active']=False
            before=len(state['calls'])
            with pytest.raises(TokenError):
                await p.exchange_refresh_token(client,refresh,['parking:read'])
            assert '/api/oidc/token' not in state['calls'][before:]
        await v.aclose()
    asyncio.run(run())


def test_provenance_persisted_compared_at_use(tmp_path):
    v,token,_state,_path=crypto_setup(tmp_path)
    app=m.build_integration(tmp_path/'bridge',v.config,'SYNTHETIC-secret',pocket_transport=v.http._transport)
    p=app.state.provider;client=downstream_client()
    async def run():
        async with app.router.lifespan_context(app):
            await p.register_client(client)
            result=await p.exchange_authorization_code(client,await seed_code(p,client,token()))
            row,_claims=p.pointer(result.access_token)
            prov=p.ledger.provenance(row['family'])
            assert prov[:2]==('SYNTHETIC-sub','SYNTHETIC-client') and len(prov[2])==64
            assert await p.load_access_token(result.access_token) is not None
            p.ledger.db.execute("UPDATE provenance SET native_grant='SYNTHETIC-other-grant'")
            assert await p.load_access_token(result.access_token) is None
        await v.aclose()
    asyncio.run(run())
