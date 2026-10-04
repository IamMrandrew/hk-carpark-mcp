"""Narrow ingress policy, not OAuth protocol implementation (library owns grants)."""
import asyncio
import json
import re
from urllib.parse import parse_qsl, urlencode

from starlette.responses import JSONResponse

from .bridge import CALLBACK, CAPACITY_EXCEEDED, RESOURCE, REVOCATION_LOOKUP
from .ledger import LimitReached

OAUTH = {'/authorize','/token','/register','/consent','/auth/callback','/revoke'}


class Policy:
    def __init__(self, app, ledger):
        self.app = app
        self.ledger = ledger

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] not in OAUTH:
            return await self.app(scope,receive,send)
        async def reject(status=400, error='invalid_request'):
            await JSONResponse({'error':error},status,headers={'Cache-Control':'no-store'})(scope,receive,send)
        if len(scope.get('query_string',b'')) > 8192:
            return await reject(413)
        body = b''
        try:
            while True:
                chunk = await asyncio.wait_for(receive(),5)
                if chunk['type'] == 'http.disconnect':
                    return
                body += chunk.get('body',b'')
                if len(body)>16384:
                    return await reject(413)
                if not chunk.get('more_body'):
                    break
        except TimeoutError:
            return await reject(408)
        path = scope['path']
        try:
            if path == '/register' and scope['method'] == 'POST':
                pairs = json.loads(body,object_pairs_hook=lambda p:p)
                # Only flat reviewed metadata; nested redirects/grants/response lists remain lists.
                data = dict(pairs)
                if len(data)!=len(pairs) or set(data)-{'redirect_uris','token_endpoint_auth_method','grant_types','response_types','scope','client_name'}:
                    return await reject()
                if data.get('redirect_uris') != [CALLBACK] or data.get('scope') not in (None,'parking:read') or data.get('token_endpoint_auth_method') not in ('none','client_secret_post') or data.get('grant_types') != ['authorization_code','refresh_token'] or data.get('response_types') != ['code'] or not isinstance(data.get('client_name',''),str) or len(data.get('client_name',''))>80:
                    return await reject()
            elif path in {'/authorize','/token'}:
                pairs = parse_qsl((scope['query_string'] if scope['method']=='GET' else body).decode('utf-8'),keep_blank_values=True, max_num_fields=24)
                data = dict(pairs)
                if len(data)!=len(pairs) or data.get('resource') != RESOURCE:
                    return await reject()
                if path=='/authorize':
                    if data.get('scope')!='parking:read' or data.get('redirect_uri')!=CALLBACK or data.get('code_challenge_method')!='S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}',data.get('code_challenge','')) or not 1 <= len(data.get('state','')) <= 256:
                        return await reject()
                else:
                    if data.get('grant_type') not in {'authorization_code','refresh_token'} or data.get('scope','parking:read')!='parking:read':
                        return await reject()
                    if data['grant_type']=='authorization_code' and data.get('redirect_uri')!=CALLBACK:
                        return await reject()
            elif path=='/revoke' and scope['method']=='POST':
                pairs = parse_qsl(body.decode('utf-8'),keep_blank_values=True,max_num_fields=8)
                data = dict(pairs)
                if len(data)!=len(pairs):
                    return await reject()
                # MCP SDK 1.30 RevocationRequest incorrectly requires this field even
                # for public clients. DCR admits ONLY 'none'; authentication is still
                # performed by the unchanged SDK handler, never bypassed here.
                data.setdefault('client_secret','')
                body = urlencode(data).encode()
        except (ValueError,TypeError,UnicodeDecodeError):
            return await reject()
        sent = False
        async def replay():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':body,'more_body':False}
            return await receive()
        try:
            context=REVOCATION_LOOKUP.set(path=='/revoke')
            capacity=CAPACITY_EXCEEDED.set(False)
            async def guarded_send(message):
                if CAPACITY_EXCEEDED.get():
                    if message['type']=='http.response.start':
                        await reject(429,'temporarily_unavailable')
                    return
                await send(message)
            try:
                await self.app(scope,replay,guarded_send)
            finally:
                REVOCATION_LOOKUP.reset(context)
                CAPACITY_EXCEEDED.reset(capacity)
        except LimitReached:
            await reject(429,'temporarily_unavailable')
