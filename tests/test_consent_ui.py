"""Consent presentation preserves FastMCP's security and submit behavior."""
import asyncio
import html
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from hkparking.auth.consent import render_consent
from hkparking.auth.integration import PUBLIC, RESOURCE
from tests.test_fixes import downstream_client
from tests.test_refresh_lifetime import pocket_fixture


def test_untrusted_labels_are_escaped():
    page = render_consent(client_name='<script>alert(1)</script>', client_id='bad"<id>',
                          redirect_uri='https://grok.com/callback?x=<img>', scopes=['parking:read'],
                          txn_id='txn"<', csrf_token='csrf"<')
    assert '<script>' not in page and '<img>' not in page
    assert '&lt;script&gt;' in page
    assert 'value="csrf&quot;&lt;"' in page
    assert 'https://grok.com/callback?x=&lt;img&gt;' in page
    assert '<script' not in page and 'https://fonts' not in page


@pytest.mark.parametrize('action,cookies,status', [
    ('approve', True, 302), ('deny', True, 302), ('approve', False, 403)])
def test_custom_consent_keeps_native_post_checks(tmp_path, action, cookies, status):
    app, verifier, _issued, _path, _transport = pocket_fixture(tmp_path)

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=PUBLIC) as http:
            client = downstream_client()
            client.client_name = 'Grok'
            await app.state.provider.register_client(client)
            response = await http.get('/bridge/authorize', params={
                'client_id': client.client_id, 'redirect_uri': str(client.redirect_uris[0]),
                'response_type': 'code', 'scope': 'parking:read', 'resource': RESOURCE,
                'state': 'SYNTHETIC-state', 'code_challenge': 'x' * 43, 'code_challenge_method': 'S256'})
            assert response.status_code == 302
            page = await http.get(response.headers['location'])
            assert page.status_code == 200
            assert 'Connect Grok<br>to Carpark?' in page.text
            assert '<span>Carpark</span>' in page.text and 'prefers-color-scheme:dark' in page.text
            assert 'Application Access Request' not in page.text
            assert page.headers['x-frame-options'] == 'DENY'
            assert page.headers['cache-control'] == 'no-store'
            assert page.headers['referrer-policy'] == 'strict-origin'
            assert int(page.headers['content-length']) == len(page.content)
            fields = {name: html.unescape(value) for name, value in
                      re.findall(r'name="([^"]+)" value="([^"]*)"', page.text)}
            if not cookies:
                http.cookies.clear()
            bad_origin = await http.post('/consent', headers={'Origin': 'null'}, data={**fields, 'action': action})
            assert bad_origin.status_code == 403 and bad_origin.text == 'Invalid Origin header'
            response = await http.post('/consent', headers={'Origin': PUBLIC}, data={**fields, 'action': action})
            assert response.status_code == status
            if status == 302:
                location = response.headers['location']
                if action == 'approve':
                    assert urlsplit(location).path == '/authorize'
                    assert 'MCP_CONSENT_BINDING' in response.headers.get('set-cookie', '')
                else:
                    assert parse_qs(urlsplit(location).query)['error'] == ['access_denied']
                    assert location.startswith(str(client.redirect_uris[0]))
            error = await http.get('/consent?txn_id=SYNTHETIC-missing')
            assert error.status_code == 400 and 'class="connection"' not in error.text
        await verifier.aclose()

    asyncio.run(run())
