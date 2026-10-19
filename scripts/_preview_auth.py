"""Repo-local, disposable LAP-45 preview adapter; NOT production OAuth.

Vendored from the reviewed compatibility spike to make releases self-contained.
Only the original provider and middleware are included; no probe CLI or tools.
Existing preview processes and grants are not restarted or modified.
"""

import asyncio
import html
import json
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.handlers.authorize import construct_redirect_uri
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

SCOPE = "parking:read"


@dataclass
class Pending:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires: float


class ManualApprovalProvider:
    def __init__(self, base: str, runtime: Path, lifetime: int):
        self.base = base.rstrip("/")
        self.resource = self.base + "/mcp"
        self.runtime = runtime
        runtime.mkdir(parents=True, exist_ok=True, mode=448)
        os.chmod(runtime, 448)
        self.deadline = time.time() + lifetime
        self.clients = {}
        self.client_expiries = {}
        self.approved_clients = set()
        self.pending = {}
        self.codes = {}
        self.access = {}
        self.refresh = {}
        self.families = {}
        self._snapshot()

    def _snapshot(self):
        self.pending = {k: v for k, v in self.pending.items() if v.expires > time.time()}
        rows = [
            {
                "request_id": k,
                "client_id": v.client.client_id,
                "client_name": v.client.client_name,
                "redirect_uri": str(v.params.redirect_uri),
                "resource": self.resource,
                "scopes": v.params.scopes,
                "expires_at": int(v.expires),
            }
            for k, v in self.pending.items()
        ]
        target = self.runtime / "pending.json"
        temporary = self.runtime / "pending.new"
        with temporary.open("w") as out:
            os.chmod(temporary, 384)
            json.dump(rows, out, ensure_ascii=True, indent=2)
        temporary.replace(target)

    def expire_unused_clients(self, now=None):
        now = time.time() if now is None else now
        for cid, expires in list(self.client_expiries.items()):
            if expires < now and cid not in self.approved_clients:
                self.clients.pop(cid, None)
                self.client_expiries.pop(cid, None)

    async def get_client(self, client_id):
        self.expire_unused_clients()
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.expire_unused_clients()
        if len(self.clients) >= 30:
            raise RegistrationError("invalid_client_metadata", "Trial client limit reached")
        for uri in client_info.redirect_uris:
            parsed = urlparse(str(uri))
            local_test = self.base.startswith("http://127.0.0.1:") and parsed.hostname == "127.0.0.1"
            if parsed.scheme != "https" and (not local_test):
                raise RegistrationError("invalid_redirect_uri", "HTTPS redirects required for public trial")
        self.clients[client_info.client_id] = client_info
        self.client_expiries[client_info.client_id] = min(time.time() + 600, self.deadline)

    async def authorize(self, client, params):
        self._snapshot()
        if len(self.pending) >= 12:
            raise AuthorizeError("temporarily_unavailable", "Too many pending requests")
        if params.resource not in (None, self.resource):
            raise AuthorizeError("invalid_request", "Incorrect resource")
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "Only parking:read is permitted")
        params = params.model_copy(update={"resource": self.resource, "scopes": [SCOPE]})
        request_id = secrets.token_hex(16)
        self.pending[request_id] = Pending(client, params, min(time.time() + 600, self.deadline))
        self._snapshot()
        return self.base + "/consent/" + request_id

    def approve_local(self, request_id):
        """Never registered as an HTTP route; local operator only."""
        if request_id not in self.pending or self.pending[request_id].expires <= time.time():
            raise ValueError("No live pending authorization request")
        marker = self.runtime / ("approve-" + request_id)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
        os.close(fd)

    async def consent(self, request):
        self._snapshot()
        rid = request.path_params["rid"]
        pending = self.pending.get(rid)
        if pending is None:
            return HTMLResponse("Authorization request expired or already completed.", status_code=410)
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        }
        marker = self.runtime / ("approve-" + rid)
        if not marker.is_file():
            content = (
                "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><meta http-equiv='refresh' content='3'><title>Clawduck MCP trial</title><h2>Waiting for local approval</h2><p>Return to your Telegram chat with Clawduck and confirm that you opened this page. Keep this browser page open. No password or verification code is needed.</p><p>This trial exposes only probe_status, not parking or device actions.</p><p>Client: "
                + html.escape(pending.client.client_name or "Unnamed")
                + "</p><p>Scope: parking:read</p><p>Request reference (not a verification code): "
                + html.escape(rid)
                + "</p><p>Callback destination: "
                + html.escape(
                    urlparse(str(pending.params.redirect_uri)).netloc
                    + urlparse(str(pending.params.redirect_uri)).path
                )
                + "</p><p>Send a screenshot of this waiting page in your trusted DM so the operator can correlate this exact request. Do not share the address-bar URL or tokens.</p><p>Approval is performed locally, never through this public page.</p>"
            )
            return HTMLResponse(content, headers=headers)
        marker.unlink()
        self.approved_clients.add(pending.client.client_id)
        code = secrets.token_urlsafe(32)
        params = pending.params
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=min(time.time() + 120, self.deadline),
            client_id=pending.client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject="local-operator-approved-trial",
        )
        del self.pending[rid]
        self._snapshot()
        redirect = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        return RedirectResponse(redirect, status_code=302, headers=headers)

    async def load_authorization_code(self, client, authorization_code):
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    def _issue(self, client_id, scopes, subject):
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.access[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(min(time.time() + 600, self.deadline)),
            resource=self.resource,
            subject=subject,
        )
        self.refresh[refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(self.deadline),
            resource=self.resource,
            subject=subject,
        )
        self.families[refresh] = access
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=int(min(600, self.deadline - time.time())),
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        found = self.codes.pop(authorization_code.code, None)
        if found is None or found.client_id != client.client_id:
            raise TokenError("invalid_grant", "Code already used or invalid")
        return self._issue(client.client_id, found.scopes, found.subject)

    async def load_refresh_token(self, client, refresh_token):
        token = self.refresh.get(refresh_token)
        return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if self.refresh.pop(refresh_token.token, None) is None:
            raise TokenError("invalid_grant", "Refresh token already used")
        old_access = self.families.pop(refresh_token.token, None)
        self.access.pop(old_access, None)
        return self._issue(client.client_id, scopes, refresh_token.subject)

    async def load_access_token(self, token):
        return self.access.get(token)

    async def revoke_token(self, token):
        if token.token in self.refresh:
            self.refresh.pop(token.token, None)
            self.access.pop(self.families.pop(token.token, None), None)
        else:
            self.access.pop(token.token, None)
            for ref, acc in list(self.families.items()):
                if acc == token.token:
                    self.families.pop(ref, None)
                    self.refresh.pop(ref, None)


class RequestBodyLimit:
    """Bound chunked bodies before any SDK OAuth or MCP parser receives them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if len(scope.get("query_string", b"")) > 8192:
            return await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, send)

        class TooLarge(Exception):
            pass

        async def collect():
            chunks = []
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > 16384:
                    raise TooLarge()
                chunks.append(chunk)
                if not message.get("more_body", False):
                    return b"".join(chunks)

        try:
            body = await asyncio.wait_for(collect(), timeout=5)
        except TooLarge:
            return await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, send)
        except TimeoutError:
            return await JSONResponse({"error": "request_timeout"}, status_code=408)(scope, receive, send)
        if body is None:
            return
        delivered = False

        async def replacement_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replacement_receive, send)


class SDKRevocationCompatibility:
    """SDK 1.x requires an optional RFC7009 field. Supply empty value, never credentials.

    Standard SDK client authentication still runs and rejects invalid/missing
    secrets for confidential clients. Only form-encoded /revoke is normalized.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/revoke" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        from urllib.parse import parse_qsl

        content_type = dict(scope["headers"]).get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if content_type != b"application/x-www-form-urlencoded":
            return await JSONResponse({"error": "unsupported_media_type"}, status_code=415)(
                scope, receive, send
            )
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if sum(map(len, chunks)) > 16384:
                return await JSONResponse({"error": "request_too_large"}, status_code=413)(
                    scope, receive, send
                )
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            fields = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True, errors="strict"))
        except (UnicodeDecodeError, ValueError):
            return await JSONResponse({"error": "invalid_request"}, status_code=400)(scope, receive, send)
        if "client_secret" not in fields:
            body += b"&client_secret="
        scope = {
            **scope,
            "headers": [(k, v) for k, v in scope["headers"] if k != b"content-length"]
            + [(b"content-length", str(len(body)).encode())],
        }
        delivered = False

        async def replacement_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replacement_receive, send)


class TrialBoundary(BaseHTTPMiddleware):
    def __init__(self, app, base, deadline):
        super().__init__(app)
        self.host = urlparse(base).netloc
        self.base = base
        self.deadline = deadline
        self.hits = deque()

    async def dispatch(self, request, call_next):
        now = time.time()
        if now >= self.deadline:
            return JSONResponse({"error": "trial_expired"}, status_code=503)
        if request.headers.get("host") != self.host:
            return JSONResponse({"error": "invalid_host"}, status_code=421)
        origin = request.headers.get("origin")
        if origin and origin != self.base:
            return JSONResponse({"error": "invalid_origin"}, status_code=403)
        path = request.url.path
        allowed = (
            path in {"/mcp", "/authorize", "/token", "/register", "/revoke"}  # noqa: PIE810 - preserve reviewed spike logic
            or path.startswith("/.well-known/")
            or path.startswith("/consent/")
        )
        if not allowed:
            return JSONResponse({"error": "not_found"}, status_code=404)
        while self.hits and self.hits[0] < now - 60:
            self.hits.popleft()
        if len(self.hits) >= 180:
            return JSONResponse({"error": "trial_rate_limit"}, status_code=429)
        self.hits.append(now)
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 16384:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        return await call_next(request)
