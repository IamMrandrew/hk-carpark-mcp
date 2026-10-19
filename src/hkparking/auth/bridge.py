"""Library-backed private OAuth bridge candidate; production adoption NOT approved."""
import base64
import json
import os
import secrets
import time
from contextvars import ContextVar
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth import TokenVerifier as UpstreamVerifier
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import AccessToken, AuthorizeError, RefreshToken, TokenError, TokenVerifier

from hkparking.deployment import SETTINGS

from .ledger import Ledger, LimitReached

PUBLIC = SETTINGS.public_origin
RESOURCE = PUBLIC + '/mcp'
CALLBACK = SETTINGS.client_redirect_uri
SCOPE = ['parking:read']
REVOCATION_LOOKUP = ContextVar('revocation_lookup',default=False)
CAPACITY_EXCEEDED = ContextVar('capacity_exceeded',default=False)


def private_keys(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory,0o700)
    path = directory / 'keys.json'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd,'w') as f:
            json.dump({'signing':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), 'fernet':Fernet.generate_key().decode()},f)
            f.flush()
            os.fsync(f.fileno())
    keys = json.loads(path.read_text())
    return base64.urlsafe_b64decode(keys['signing']), keys['fernet'].encode()


class SyntheticIdentityVerifier(UpstreamVerifier):
    """ONLY for loopback fixture; not production identity authorization."""
    def __init__(self, upstream):
        super().__init__(required_scopes=SCOPE)
        self.upstream = upstream

    async def verify_token(self, token):
        async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
            r = await c.post(self.upstream+'/introspect',data={'token':token})
            r.raise_for_status()
            claims = r.json()
        if not claims.get('active') or claims.get('sub') != 'synthetic-test-subject' or claims.get('scope') != 'parking:read' or claims.get('resource') != RESOURCE or claims.get('client_id') != 'synthetic-upstream-app':
            return None
        return AccessToken(token=token,client_id=claims['client_id'],scopes=SCOPE,expires_at=claims['exp'],resource=RESOURCE)


class TransactionStore:
    """Quota wrapper around the supported SDK Pydantic store; no OAuth rewrite."""
    def __init__(self, store, ledger):
        self.store, self.ledger = store, ledger

    async def put(self, key, value, *, ttl=None):
        row = self.ledger.db.execute('SELECT exp FROM transactions WHERE id=?', (key,)).fetchone()
        deadline = min(value.created_at + 900, row['exp']) if row else value.created_at + 900
        # Consent updates may shorten this deadline, never move it forward.
        remaining = int(deadline - time.time())
        if remaining <= 0:
            await self.delete(key=key)
            raise AuthorizeError('invalid_request', 'Authorization transaction expired')
        ttl = remaining if ttl is None else min(ttl, remaining)
        expired = self.ledger.admit_transaction(key,value.client_id,deadline)
        for old in expired:
            await self.store.delete(key=old)
        try:
            await self.store.put(key=key,value=value,ttl=ttl)
        except BaseException:
            self.ledger.db.execute('DELETE FROM transactions WHERE id=?',(key,))
            raise

    async def get(self, key):
        value = await self.store.get(key=key)
        row = self.ledger.db.execute('SELECT exp FROM transactions WHERE id=?', (key,)).fetchone()
        if value is None or row is None or min(value.created_at + 900, row['exp']) <= time.time():
            await self.delete(key=key)
            return None
        return value

    async def delete(self, key):
        result = await self.store.delete(key=key)
        self.ledger.db.execute('DELETE FROM transactions WHERE id=?',(key,))
        return result


class Bridge(OAuthProxy):
    async def authorize(self,client,params):
        # SDK has already validated the client/request before calling this hook.
        # Also fail closed for callers that invoke the provider directly.
        if (await self.get_client(client.client_id) is None or params.scopes!=SCOPE
                or str(params.redirect_uri)!=CALLBACK or str(params.resource)!=RESOURCE
                or not params.code_challenge):
            raise AuthorizeError('invalid_request','Authorization request rejected')
        try:
            return await super().authorize(client,params)
        except LimitReached:
            CAPACITY_EXCEEDED.set(True)
            raise

    async def register_client(self,client_info):
        expired = self.ledger.admit('registration',client_info.client_id,16,86400)
        for client in expired:
            await self._client_store.delete(key=client)
        await super().register_client(client_info)

    async def get_client(self,client_id):
        if not self.ledger.registered(client_id):
            return None
        return await super().get_client(client_id)

    def pointer(self,token,kind='access'):
        try:
            claims = self.jwt_issuer.verify_token(token,expected_token_use=kind)
            row = self.ledger.lookup(claims['jti'])
            if row and row['kind']==kind and row['client']==claims['client_id'] and claims.get('scope')=='parking:read':
                return row,claims
        except Exception:  # noqa: BLE001, S110 - any failure must deny (fail closed), no error body leak
            pass
        return None,None

    async def record(self,result,client,reserved=None):
        payloads = [self.jwt_issuer.verify_token(result.access_token)]
        if result.refresh_token:
            payloads.append(self.jwt_issuer.verify_token(result.refresh_token,expected_token_use='refresh'))
        mapping = await self._jti_mapping_store.get(key=payloads[0]['jti'])
        provenance = await self.verified_provenance(mapping.upstream_token_id)
        self.ledger.record(mapping.upstream_token_id,client.client_id,payloads,reserved,provenance=provenance)
        self.ledger.approved(client.client_id)
        return result

    async def verified_provenance(self,family):
        """Optional integration hook; base bridge lifecycle/protocol is unchanged."""
        return

    async def exchange_authorization_code(self,client,authorization_code):
        if authorization_code.client_id!=client.client_id or not self.ledger.consume_code(authorization_code.code,authorization_code.expires_at):
            raise TokenError('invalid_grant','Code is unavailable')
        try:
            return await self.record(await super().exchange_authorization_code(client,authorization_code),client)
        except Exception:  # noqa: BLE001 - scrub upstream detail, always deny
            raise TokenError('invalid_grant','Code exchange failed') from None

    async def exchange_refresh_token(self,client,refresh_token,scopes):
        row,claims = self.pointer(refresh_token.token,'refresh')
        if row is None or row['client']!=client.client_id or scopes!=SCOPE:
            raise TokenError('invalid_grant','Grant is unavailable')
        outcome = self.ledger.reserve(claims['jti'],client.client_id)
        if outcome=='replay':
            await self.revoke_token(refresh_token)
        if outcome!='reserved':
            raise TokenError('invalid_grant','Grant is unavailable')
        try:
            await self.before_refresh(row)
            result = await super().exchange_refresh_token(client,refresh_token,scopes)
            return await self.record(result,client,reserved=claims['jti'])
        except BaseException:  # noqa: BLE001 - includes cancellation; any failure revokes family
            # SIGKILL leaves 'pending', which also denies all use.
            self.ledger.revoke(row['family'])
            raise TokenError('invalid_grant','Refresh failed; reconnect required') from None

    async def before_refresh(self,row):
        """Optional current-native-grant integration gate, after local reservation."""
        return

    async def load_access_token(self,token):
        row,claims = self.pointer(token)
        if row is None:
            return None
        if REVOCATION_LOOKUP.get():
            # SDK revoke handler still authenticates client and compares client_id.
            # Do not require 'live' or upstream network validation to revoke a grant.
            return AccessToken(token=token,client_id=row['client'],scopes=SCOPE,expires_at=claims['exp'],resource=RESOURCE)
        if row['status']!='live':
            return None
        upstream = await self._upstream_token_store.get(key=row['family'])
        if upstream is None or upstream.expires_at <= time.time():
            return None
        # Never invoke OAuthProxy's unguarded transparent-refresh path. Only
        # explicit refresh_token exchanges own durable family reservations.
        result = await self._token_validator.verify_token(upstream.access_token)
        # Recheck after awaited upstream validation so concurrent revoke wins.
        current = await self._upstream_token_store.get(key=row['family'])
        live = self.ledger.lookup(claims['jti'])
        now = time.time()
        if (result is None or result.scopes!=SCOPE or result.resource!=RESOURCE
                or live is None or live['status']!='live' or current is None
                or current.access_token!=upstream.access_token
                or current.expires_at<=now or upstream.expires_at<=now
                or result.expires_at is None or result.expires_at<=now or claims['exp']<=now):
            return None
        return result.model_copy(update={'token':token,'client_id':row['client'],'scopes':claims['scope'].split(),'expires_at':min(claims['exp'],result.expires_at,int(current.expires_at)),'resource':RESOURCE})

    async def load_refresh_token(self,client,token):
        row,_ = self.pointer(token,'refresh')
        if row is None or row['client']!=client.client_id:
            return None
        if REVOCATION_LOOKUP.get():
            return RefreshToken(token=token,client_id=row['client'],scopes=SCOPE,expires_at=row['exp'])
        if row['status']!='live':
            return None
        if row['spent']:
            await self.revoke_token(RefreshToken(token=token,client_id=client.client_id,scopes=SCOPE,expires_at=row['exp']))
            return None
        return await super().load_refresh_token(client,token)

    async def revoke_token(self,token):
        kind = 'refresh' if isinstance(token,RefreshToken) else 'access'
        row,_ = self.pointer(token.token,kind)
        if row is None:
            return
        self.ledger.revoke(row['family'])
        await self.flush_revocations()

    async def flush_revocations(self):
        if not self._upstream_revocation_endpoint:
            return
        families = list(self.ledger.db.execute('SELECT family,generation FROM revoke_outbox'))
        async with httpx.AsyncClient(trust_env=False,timeout=5) as c:
            for family,generation in families:
                try:
                    upstream = await self._upstream_token_store.get(key=family)
                    if upstream is None:
                        continue
                    for value,hint in ((upstream.refresh_token,'refresh_token'),(upstream.access_token,'access_token')):
                        if value:
                            data = {'token':value,'token_type_hint':hint}
                            auth = None
                            if self._upstream_client_secret is not None:
                                auth = (self._upstream_client_id,self._upstream_client_secret.get_secret_value())
                            else:
                                data['client_id']=self._upstream_client_id
                            r = await c.post(self._upstream_revocation_endpoint,data=data,auth=auth)
                            r.raise_for_status()
                    self.ledger.db.execute('DELETE FROM revoke_outbox WHERE family=? AND generation=?',(family,generation))
                except Exception:  # noqa: BLE001, S112 - durable retry, never leak upstream errors
                    # Local revocation already
                    # committed; never emit upstream error bodies or tokens to logs.
                    continue



class ResourceVerifier(TokenVerifier):
    def __init__(self, provider):
        self.provider = provider

    async def verify_token(self, token):
        result = await self.provider.load_access_token(token)
        if result is None:
            return None
        claims = self.provider.jwt_issuer.verify_token(token)
        return result.model_copy(update={'token':token,'client_id':claims['client_id'],'resource':RESOURCE})


def make_provider(directory, *, token_verifier, upstream_authorization_endpoint,
                  upstream_token_endpoint, upstream_revocation_endpoint,
                  upstream_client_id, upstream_client_secret,
                  provider_class=Bridge, provider_options=None):
    """Explicit integration entry point: caller owns immutable-subject IdP policy.

    OAuthProxy still owns HTTP OAuth, PKCE, consent, token issuance and crypto.
    No production identity/verifier/credential is selected or generated here.
    """
    if token_verifier is None or not callable(getattr(token_verifier,'verify_token',None)):
        raise TypeError('An explicit upstream token verifier is required')
    from .safe_logging import install_redaction
    install_redaction()
    signing, encryption = private_keys(directory)
    disk = DiskStore(directory=Path(directory)/'encrypted-library-store')
    store = FernetEncryptionWrapper(disk,fernet=Fernet(encryption))
    provider = provider_class(
        upstream_authorization_endpoint=upstream_authorization_endpoint,
        upstream_token_endpoint=upstream_token_endpoint,
        upstream_revocation_endpoint=upstream_revocation_endpoint,
        upstream_client_id=upstream_client_id,
        upstream_client_secret=upstream_client_secret,
        token_verifier=token_verifier,
        base_url=PUBLIC, client_storage=store, jwt_signing_key=signing,
        allowed_client_redirect_uris=[CALLBACK], valid_scopes=SCOPE,
        enable_cimd=False, require_authorization_consent=True,
        fastmcp_access_token_expiry_seconds=300,
        fallback_refresh_token_expiry_seconds=3600,
        **(provider_options or {}),
    )
    provider.set_mcp_path('/mcp')
    provider.ledger = Ledger(Path(directory)/'grants.sqlite')
    provider._transaction_store = TransactionStore(provider._transaction_store,provider.ledger)
    provider.disk = disk
    return provider


def make_bridge(directory, upstream):
    """SYNTHETIC loopback test factory. NEVER use this for production identity."""
    from urllib.parse import urlsplit
    parsed=urlsplit(upstream)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.path:
        raise ValueError('Synthetic fixture must be loopback-only')
    return make_provider(
        directory,
        upstream_authorization_endpoint=upstream+'/authorize',
        upstream_token_endpoint=upstream+'/token',
        upstream_revocation_endpoint=upstream+'/revoke',
        upstream_client_id='synthetic-upstream-app',
        upstream_client_secret='synthetic-fixture-secret',
        token_verifier=SyntheticIdentityVerifier(upstream),
    )
