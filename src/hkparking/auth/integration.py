"""Reviewable Pocket ID v2.17.0 integration. No environment/credential discovery."""
import asyncio
import re
import sqlite3
import time
from dataclasses import dataclass

import httpx
import jwt
from fastmcp.server.auth import TokenVerifier
from mcp.server.auth.provider import AccessToken

from .bridge import PUBLIC, RESOURCE, SCOPE
from .consent import render_consent
from .native_status import RELEASE, NativeStatus

__all__ = ['RELEASE', 'NativeStatus', 'PocketConfig', 'PocketVerifier', 'build_integration']


@dataclass(frozen=True)
class PocketConfig:
    allowed_sub: str
    client_id: str
    loopback_port: int
    identity_database: str | None = None
    native_release: str | None = None
    identity_file: tuple[int,int] | None = None

    def __post_init__(self):
        if not isinstance(self.allowed_sub, str) or not self.allowed_sub or self.allowed_sub.startswith('client-'):
            raise ValueError('Explicit immutable user subject required')
        if not isinstance(self.client_id, str) or not self.client_id or '://' in self.client_id:
            raise ValueError('Explicit preconfigured local client required; no CIMD')
        if type(self.loopback_port) is not int or not 1 <= self.loopback_port <= 65535:
            raise ValueError('Explicit loopback port required')


class PocketTransport(httpx.AsyncBaseTransport):
    """No DNS, proxies, redirects, supplied URLs or unbounded response buffering."""
    def __init__(self, config, paths):
        self.config = config
        self.paths = frozenset(paths)
        self.inner = httpx.AsyncHTTPTransport(retries=0, limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))

    async def handle_async_request(self, request):
        permitted = request.url.path in self.paths or self.paths == frozenset(PUBLIC_PATHS) and public_path(request.url.path,request.method)
        if str(request.url.copy_with(path='/', query=None, fragment=None)) != PUBLIC+'/' or not permitted:
            raise ValueError('Destination not explicitly configured')
        url = request.url.copy_with(scheme='http', host='127.0.0.1', port=self.config.loopback_port)
        headers = [(k,v) for k,v in request.headers.raw if k.lower() not in {b'host',b'x-forwarded-host',b'x-forwarded-proto',b'forwarded'}]
        headers += [(b'host', b'carpark.laporatory.com'), (b'x-forwarded-proto',b'https')]
        response = await self.inner.handle_async_request(httpx.Request(request.method,url,headers=headers,stream=request.stream,extensions=request.extensions))
        content = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 262144:
                    raise ValueError('Pocket response exceeds bound')
        finally:
            await response.aclose()
        # aiter_bytes() already decoded content-encoding; the header must NOT be forwarded
        clean_headers = [(k, v) for k, v in response.headers.raw
                         if k.lower() not in (b'content-encoding', b'content-length', b'transfer-encoding')]
        return httpx.Response(response.status_code, headers=clean_headers, content=bytes(content))

    async def aclose(self):
        await self.inner.aclose()


class PocketVerifier(TokenVerifier):
    """RFC9068 crypto plus current issuer grant/user state, never an ID-token verifier."""
    def __init__(self, config, client_secret, *, transport=None):
        super().__init__(required_scopes=SCOPE)
        self._config = config
        self.status = NativeStatus(config)
        self.http = httpx.AsyncClient(transport=transport or PocketTransport(config, {
            '/.well-known/jwks.json','/api/oidc/introspect','/api/oidc/userinfo'}),
            trust_env=False, follow_redirects=False, timeout=5)
        self.secret = client_secret
        self.keys = None
        self.keys_at = 0

    @property
    def config(self):
        return self._config

    @property
    def identity_database(self):
        return self.config.identity_database

    async def verify_token(self, token):
        try:
            if not isinstance(token,str) or len(token)>16384:
                return None
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or header.get('typ') != 'at+jwt' or not isinstance(header.get('kid'),str):
                return None
            if self.keys is None or time.monotonic()-self.keys_at > 60:
                r = await self.http.get(PUBLIC+'/.well-known/jwks.json')
                r.raise_for_status()
                self.keys = jwt.PyJWKSet.from_dict(r.json())
                self.keys_at = time.monotonic()
            matches = [k for k in self.keys.keys if k.key_id == header['kid'] and k.algorithm_name == 'RS256' and k.public_key_use == 'sig']
            if len(matches) != 1:
                return None
            claims = jwt.decode(token,matches[0].key,algorithms=['RS256'],issuer=PUBLIC,audience=PUBLIC,
                options={'require':['iss','aud','sub','exp','iat','jti','client_id']}, leeway=0)
            if claims['sub'] != self.config.allowed_sub or claims['client_id'] != self.config.client_id or claims.get('scope') != 'openid':
                return None
            audience = claims['aud'] if isinstance(claims['aud'],list) else [claims['aud']]
            if set(audience) != {PUBLIC,self.config.client_id}:
                return None
            r = await self.http.post(PUBLIC+'/api/oidc/introspect',data={'token':token,'token_type_hint':'access_token'},
                auth=(self.config.client_id,self.secret))
            r.raise_for_status()
            if r.json().get('active') is not True:
                return None
            r = await self.http.get(PUBLIC+'/api/oidc/userinfo',headers={'Authorization':'Bearer '+token})
            r.raise_for_status()
            if r.json().get('sub') != self.config.allowed_sub:
                return None
            provenance = self.status.grant(token,claims)
            if provenance is None:
                return None
            if type(claims['exp']) is not int or claims['exp'] <= time.time():
                return None
            return AccessToken(token=token,client_id=self.config.client_id,scopes=SCOPE,expires_at=claims['exp'],resource=RESOURCE,subject=claims['sub'],claims={'native_grant':provenance})
        except (ValueError,TypeError,KeyError,OSError,httpx.HTTPError,jwt.PyJWTError,sqlite3.Error):
            return None

    async def aclose(self):
        await self.http.aclose()


from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import parse_qsl

from authlib.integrations.httpx_client import AsyncOAuth2Client
from mcp.server.auth.settings import RevocationOptions
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .bridge import Bridge, ResourceVerifier, make_provider
from .policy import Policy

NATIVE_WRITES = ContextVar('native_issuance_writes',default=None)


class NativeTokenStore:
    """Verification BEFORE SDK persistence/minting; no custom OAuth protocol."""
    def __init__(self,store,provider):
        self.store,self.provider=store,provider

    async def put(self,key,value,*,ttl=None):
        await self.provider.validate_native_set(key,value)
        writes=NATIVE_WRITES.get()
        if writes is not None:
            writes.append(key)
        return await self.store.put(key=key,value=value,ttl=ttl)

    async def get(self,key):
        return await self.store.get(key=key)

    async def delete(self,key):
        return await self.store.delete(key=key)


class PocketOAuthClient(AsyncOAuth2Client):
    """Supply the native refresh deadline omitted by Pocket's token response."""
    def __init__(self,*,normalize_tokens,**kwargs):
        self.normalize_tokens=normalize_tokens
        super().__init__(**kwargs)

    async def fetch_token(self,*args,**kwargs):
        return self.normalize_tokens(await super().fetch_token(*args,**kwargs))

    async def refresh_token(self,*args,**kwargs):
        return self.normalize_tokens(await super().refresh_token(*args,**kwargs))


class PocketBridge(Bridge):
    async def _show_consent_page(self, request):
        # Let FastMCP validate the request, issue CSRF state and set its cookies.
        response = await super()._show_consent_page(request)
        if response.status_code != 200:
            return response
        txn_id = request.query_params.get('txn_id')
        txn = await self._transaction_store.get(key=txn_id)
        if txn is None or not txn.csrf_token:
            return response
        client = await self.get_client(txn.client_id)
        response.body = render_consent(
            client_name=getattr(client, 'client_name', None), client_id=txn.client_id,
            redirect_uri=txn.client_redirect_uri, scopes=txn.scopes or [],
            txn_id=txn_id, csrf_token=txn.csrf_token).encode('utf-8')
        response.headers['content-length'] = str(len(response.body))
        response.headers['cache-control'] = 'no-store'
        response.headers['referrer-policy'] = 'no-referrer'
        return response

    def normalize_native_tokens(self,tokens):
        from mcp.server.auth.provider import TokenError
        tokens=dict(tokens)
        if not tokens.get('refresh_token'):
            return tokens
        try:
            deadline=self._token_validator.status.refresh_expiry(tokens['refresh_token'])
            remaining=int(deadline-time.time()) if deadline is not None else 0
            # Honor a shorter explicit issuer lifetime as well as the native deadline.
            declared=int(tokens.get('refresh_expires_in',remaining))
            remaining=min(remaining,declared)
            if remaining<=0:
                raise ValueError('Native refresh grant unavailable')
            tokens['refresh_expires_in']=remaining
            return tokens
        except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):
            raise TokenError('invalid_grant','Native refresh grant unavailable') from None

    async def _handle_idp_callback(self,request):
        if request.query_params.get('error'):
            return Response('Authorization failed; reconnect required',status_code=400,headers={'Cache-Control':'no-store'})
        response=await super()._handle_idp_callback(request)
        if response.status_code>=400:
            return Response('Authorization failed; reconnect required',status_code=response.status_code,headers={'Cache-Control':'no-store'})
        return response

    async def validate_native_set(self,key,value):
        from mcp.server.auth.provider import TokenError
        result=await self._token_validator.verify_token(value.access_token)
        if result is None or result.expires_at is None or min(result.expires_at,value.expires_at)<=time.time():
            raise TokenError('invalid_grant','Native grant unavailable')
        provenance=(result.subject,result.client_id,result.claims['native_grant'])
        prior=self.ledger.provenance(key)
        exists=self.ledger.db.execute('SELECT 1 FROM families WHERE id=?',(key,)).fetchone()
        if exists and prior!=provenance:
            raise TokenError('invalid_grant','Native grant provenance changed')
        return provenance

    async def verified_provenance(self,family):
        value=await self._upstream_token_store.get(key=family)
        if value is None:
            raise ValueError('Native grant unavailable')
        return await self.validate_native_set(family,value)

    async def before_refresh(self,row):
        from mcp.server.auth.provider import TokenError
        value=await self._upstream_token_store.get(key=row['family'])
        expected=self.ledger.provenance(row['family'])
        if value is None or not value.refresh_token or expected is None:
            raise TokenError('invalid_grant','Native grant unavailable')
        v=self._token_validator
        r=await v.http.post(PUBLIC+'/api/oidc/introspect',data={'token':value.refresh_token,'token_type_hint':'refresh_token'},auth=(v.config.client_id,v.secret))
        r.raise_for_status()
        if r.json().get('active') is not True or not v.status.refresh(value.refresh_token,expected):
            raise TokenError('invalid_grant','Native grant unavailable')
        current=self.ledger.db.execute('SELECT status FROM families WHERE id=?',(row['family'],)).fetchone()
        if current is None or current[0]!='pending':
            raise TokenError('invalid_grant','Grant unavailable')

    async def load_access_token(self,token):
        result=await super().load_access_token(token)
        if result is None:
            return None
        from .bridge import REVOCATION_LOOKUP
        if not REVOCATION_LOOKUP.get():
            row,_=self.pointer(token)
            if row is None or self.ledger.provenance(row['family'])!=(result.subject,self._upstream_client_id,(result.claims or {}).get('native_grant')):
                return None
        return result

    async def _issue_guard(self,operation):
        context=NATIVE_WRITES.set([])
        try:
            return await operation()
        except BaseException:
            # Only initial unowned staging; existing-family revocation material
            # lifecycle belongs to auth-final LOGIC1, not this integration fix.
            for key in NATIVE_WRITES.get():
                if self.ledger.db.execute('SELECT 1 FROM families WHERE id=?',(key,)).fetchone() is None:
                    await self._upstream_token_store.delete(key=key)
            raise
        finally:
            NATIVE_WRITES.reset(context)

    async def exchange_authorization_code(self,client,authorization_code):
        try:
            # Consent/code delivery can take time. Recompute immediately before
            # the SDK derives JWT/storage TTLs rather than extending a stale TTL.
            code=await self._code_store.get(key=authorization_code.code)
            if code is not None:
                code.idp_tokens=self.normalize_native_tokens(code.idp_tokens)
                ttl=int(code.expires_at-time.time())
                if ttl<=0:
                    from mcp.server.auth.provider import TokenError
                    raise TokenError('invalid_grant','Code is unavailable')
                await self._code_store.put(key=code.code,value=code,ttl=ttl)
            return await self._issue_guard(lambda:super(PocketBridge,self).exchange_authorization_code(client,authorization_code))
        finally:
            await self._code_store.delete(key=authorization_code.code)

    async def exchange_refresh_token(self,client,refresh_token,scopes):
        return await self._issue_guard(lambda:super(PocketBridge,self).exchange_refresh_token(client,refresh_token,scopes))

    def _prepare_scopes_for_token_exchange(self, scopes):
        if scopes != SCOPE:
            raise ValueError('Invalid downstream scope')
        return ['openid']

    _prepare_scopes_for_upstream_refresh = _prepare_scopes_for_token_exchange

    def _translate_scopes_from_idp(self, scopes):
        if scopes != ['openid']:
            raise ValueError('Invalid upstream scope')
        return SCOPE.copy()

    def _create_upstream_oauth_client(self):
        return PocketOAuthClient(normalize_tokens=self.normalize_native_tokens,client_id=self._upstream_client_id,
            client_secret=self._upstream_client_secret.get_secret_value(),
            token_endpoint_auth_method='client_secret_basic', transport=self.pocket_transport,
            timeout=5,trust_env=False,follow_redirects=False)

    def get_routes(self, mcp_path=None):
        routes = super().get_routes(mcp_path)
        result=[]
        for route in routes:
            if route.path == '/authorize':
                result.append(Route('/bridge/authorize',endpoint=route.endpoint,methods=list(route.methods)))
            elif route.path == '/.well-known/oauth-authorization-server':
                from mcp.server.auth.routes import build_metadata
                metadata = build_metadata(self.base_url,self.service_documentation_url,self.client_registration_options,self.revocation_options)
                async def discovery(request, metadata=metadata, PUBLIC=PUBLIC, SCOPE=SCOPE):
                    data = metadata.model_dump(mode='json',exclude_none=True)
                    data['authorization_endpoint'] = PUBLIC+'/bridge/authorize'
                    data['scopes_supported'] = SCOPE
                    return JSONResponse(data,headers={'Cache-Control':'no-store'})
                result.append(Route(route.path,discovery,methods=['GET','OPTIONS']))
            else:
                result.append(route)
        return result


class IntegrationPolicy:
    def __init__(self,app,provider):
        self.app = app
        self.provider = provider
        self.policy = Policy(app,ledger=provider.ledger)

    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and scope['path']=='/bridge/authorize':
            # Policy validates the existing reviewed downstream authorize semantics;
            # restore the new path before handing to the library router.
            original = scope
            async def dispatch(_scope,_receive,_send):
                await self.app(original,_receive,_send)
            await Policy(dispatch,self.provider.ledger)({**scope,'path':'/authorize'},receive,send)
        elif scope['type']=='http' and scope['path']=='/authorize':
            await self.app(scope,receive,send)
        else:
            await self.policy(scope,receive,send)


def build_integration(directory,config,client_secret,*,pocket_transport=None):
    if not config.identity_database:
        raise ValueError('Explicit local read-only Pocket ID identity database required')
    from hkparking.app import build_app
    from hkparking.config import ServerConfig
    verifier = PocketVerifier(config,client_secret,transport=pocket_transport)
    provider = make_provider(directory,token_verifier=verifier,
        upstream_authorization_endpoint=PUBLIC+'/authorize',
        upstream_token_endpoint=PUBLIC+'/api/oidc/token',upstream_revocation_endpoint=None,
        upstream_client_id=config.client_id,upstream_client_secret=client_secret,
        provider_class=PocketBridge,provider_options={'forward_resource':False,'extra_authorize_params':{'scope':'openid'}})
    provider.pocket_transport = pocket_transport or PocketTransport(config,{'/api/oidc/token'})
    provider._upstream_token_store = NativeTokenStore(provider._upstream_token_store,provider)
    # Only local RFC7009 family denial is supported. NEVER forward proxy JWTs.
    provider.revocation_options = RevocationOptions(enabled=True)
    app = build_app(ServerConfig(issuer_url=PUBLIC),ResourceVerifier(provider))
    app.router.routes[0:0] = [r for r in provider.get_routes('/mcp') if not r.path.startswith('/.well-known/oauth-protected-resource')]
    app.state.provider = provider
    http = httpx.AsyncClient(transport=pocket_transport or PocketTransport(config, PUBLIC_PATHS),
        trust_env=False,follow_redirects=False,timeout=5)

    async def pocket(request):
        path = request.url.path
        if not public_path(path,request.method):
            return Response(status_code=404)
        async def interaction_allowed(iid):
            try:
                q=verifier.status.interaction(iid)
                if not native_authorize_query(q,config):
                    return False
                check=await http.get(PUBLIC+'/api/oidc/interactions/'+iid,headers={'Cookie':request.headers.get('cookie','')})
                if check.status_code!=200:
                    return False
                data=check.json()
                if data.get('id')!=iid or data.get('client',{}).get('id')!=config.client_id or data.get('scopes')!=['openid']:
                    return False
                if path.endswith('/complete'):
                    owner=await http.get(PUBLIC+'/api/users/me',headers={'Cookie':request.headers.get('cookie','')})
                    if owner.status_code!=200 or owner.json().get('id')!=config.allowed_sub:
                        return False
                # Re-read after awaits to deny status/schema/provenance drift.
                return verifier.status.interaction(iid)==q
            except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,httpx.HTTPError):
                return False
        match=re.fullmatch(r'/api/oidc/interactions/([A-Za-z0-9-]{36})(/complete)?',path)
        if match and not await interaction_allowed(match[1]):
            return Response(status_code=400)
        if path=='/authorize':
            try:
                pairs=parse_qsl(request.url.query,keep_blank_values=True,max_num_fields=12)
                q=dict(pairs)
                if len(pairs)==1 and re.fullmatch('[A-Za-z0-9-]{36}',q.get('interaction','')):
                    if not await interaction_allowed(q['interaction']):
                        return Response(status_code=400)
                else:
                    if len(pairs)!=len(q) or len(request.url.query)>8192 or set(q)-{'client_id','redirect_uri','response_type','scope','state','code_challenge','code_challenge_method'}:
                        return Response(status_code=400)
                    if not native_authorize_query(q,config):
                        return Response(status_code=400)
            except ValueError:
                return Response(status_code=400)
        body=bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body)>16384:
                        return Response(status_code=413)
        except TimeoutError:
            return Response(status_code=408)
        headers=[(k,v) for k,v in request.headers.raw if k.lower() in {b'content-type',b'cookie',b'origin',b'accept'}]
        r=await http.request(request.method,PUBLIC+path+('?' + request.url.query if request.url.query else ''),content=bytes(body),headers=headers)
        response=Response(r.content,status_code=r.status_code)
        response.raw_headers=[(k,v) for k,v in r.headers.raw if k.lower() not in {b'connection',b'transfer-encoding',b'content-length',b'content-encoding'}]
        return response

    app.router.routes.append(Route('/{rest:path}',pocket,methods=['GET','POST']))
    # Existing Host/Origin boundary MUST stay outermost before any state/network work.
    app.user_middleware.append(Middleware(IntegrationPolicy,provider=provider))
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original(app):
                yield
        finally:
            await http.aclose()
            await verifier.aclose()
            await provider.pocket_transport.aclose()
            provider.disk._cache.close()
            provider.ledger.db.close()
    app.router.lifespan_context=lifespan
    return app


PUBLIC_PATHS = {'/','/login','/interaction','/authorize','/.well-known/openid-configuration',
    '/.well-known/jwks.json','/api/application-configuration','/api/users/me',
    '/api/webauthn/login/start','/api/webauthn/login/finish','/api/webauthn/logout'}


def native_authorize_query(q,config):
    return (isinstance(q,dict) and q.get('client_id')==config.client_id
        and q.get('redirect_uri')==PUBLIC+'/auth/callback' and q.get('response_type')=='code'
        and q.get('scope')=='openid' and q.get('code_challenge_method')=='S256'
        and re.fullmatch('[A-Za-z0-9_-]{43}',q.get('code_challenge','')) is not None
        and 1<=len(q.get('state',''))<=256
        and not set(q)-{'client_id','redirect_uri','response_type','scope','state','code_challenge','code_challenge_method'})


def public_path(path,method):
    import re
    if path in PUBLIC_PATHS:
        return method=='GET' or path in {'/api/webauthn/login/start','/api/webauthn/login/finish','/api/webauthn/logout'} and method=='POST'
    if path in ('/app.webmanifest','/favicon.ico','/favicon.png','/favicon.svg'):
        return method=='GET'
    if re.fullmatch(r'/img/[A-Za-z0-9_./-]+\.(png|svg|jpg|webp|ico)', path):
        return method=='GET' and '..' not in path
    if re.fullmatch(r'/api/oidc/interactions/[A-Za-z0-9-]{36}',path):
        return method=='GET'
    if re.fullmatch(r'/api/oidc/interactions/[A-Za-z0-9-]{36}/complete',path):
        return method=='POST'
    if re.fullmatch(r'/api/application-images/(logo|background|favicon|email|dark-logo)',path):
        return method=='GET'
    if path=='/interaction/error' or path=='/api/webauthn/reauthenticate':
        return True
    # Authenticated user dashboard (SPA shell). Pocket serves these via its
    # frontend NoRoute handler; the browser lands here after passkey login.
    if path == '/settings' or (re.fullmatch(r'/settings/[A-Za-z0-9_-]+', path) and path != '/settings/admin'):
        # /settings/admin stays denied: the admin UI is useless without the
        # admin APIs, which remain blocked by default.
        return method == 'GET'
    # User-scoped account APIs (Pocket enforces userAuth cookies itself).
    if path == '/api/users/me':
        return method in ('GET', 'PUT')
    if path == '/api/users/me/profile-picture':
        return method in ('PUT', 'DELETE')
    if path == '/api/webauthn/credentials':
        return method == 'GET'
    if re.fullmatch(r'/api/webauthn/credentials/[A-Za-z0-9-]+', path):
        return method in ('PATCH', 'DELETE')
    if path == '/api/oidc/users/me/authorized-clients' or path == '/api/oidc/users/me/clients':
        return method == 'GET'
    if re.fullmatch(r'/api/oidc/users/me/authorized-clients/[A-Za-z0-9._-]+', path):
        return method == 'DELETE'
    if re.fullmatch(r'/_app/immutable/[A-Za-z0-9_./-]+\.(js|css|woff2|svg|png)',path):
        return method=='GET' and '..' not in path
    return False
