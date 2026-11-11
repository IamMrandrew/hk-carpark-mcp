"""Bounded DCR metadata validation. URLs are metadata, never fetched."""
import ipaddress
import re
from urllib.parse import unquote, urlsplit

from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from mcp.shared.auth import OAuthClientInformationFull

AUTH_METHODS = {'none', 'client_secret_post', 'client_secret_basic'}
URL_FIELDS = {'client_uri', 'logo_uri', 'tos_uri', 'policy_uri'}
TEXT_FIELDS = {'client_name':80, 'software_id':256, 'software_version':128}
FIELDS = {'redirect_uris', 'token_endpoint_auth_method', 'grant_types', 'response_types',
          'scope', 'contacts'} | URL_FIELDS | TEXT_FIELDS.keys()


def client_redirect_allowed(uri):
    if not isinstance(uri, str) or not 1 <= len(uri) <= 2048:
        return False
    if any(c.isspace() or ord(c) < 32 for c in uri) or any(c in unquote(uri) for c in ('*', '\\', '#')):
        return False
    try:
        parsed = urlsplit(uri)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or '#' in uri or not host or parsed.port == 0):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        # No DNS lookup: reject non-public/special-use names syntactically.
        host = host.rstrip('.').lower()
        if host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')) or host == 'localhost':
            return False
        return '.' in host and all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                                  for label in host.split('.')) and not host.replace('.', '').isdigit()
    except ValueError:
        return False


def validate_metadata(data):
    """Return normalized metadata or reject; do not widen requested grants."""
    if not isinstance(data, dict) or set(data) - FIELDS:
        raise ValueError('Unsupported client metadata')
    redirects = data.get('redirect_uris')
    if (not isinstance(redirects, list) or not 1 <= len(redirects) <= 4
            or not all(client_redirect_allowed(uri) for uri in redirects)):
        raise ValueError('Public HTTPS redirect_uris are required')
    method = data.get('token_endpoint_auth_method', 'client_secret_basic')
    if method not in AUTH_METHODS or data.get('scope') not in (None, 'parking:read'):
        raise ValueError('Unsupported authentication method or scope')
    grants = data.get('grant_types', ['authorization_code'])
    responses = data.get('response_types', ['code'])
    if (not isinstance(grants, list) or len(grants) != 2 or set(grants) != {'authorization_code', 'refresh_token'}):
        raise ValueError('This server requires grant_types authorization_code and refresh_token')
    if responses != ['code']:
        raise ValueError('Only response_type code is supported')
    for field, bound in TEXT_FIELDS.items():
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > bound):
            raise ValueError('Invalid text metadata')
    for field in URL_FIELDS:
        value = data.get(field)
        if value is not None and not client_redirect_allowed(value):
            raise ValueError('Invalid URL metadata')
    contacts = data.get('contacts')
    if contacts is not None and (not isinstance(contacts, list) or len(contacts) > 4
                                or not all(isinstance(v, str) and len(v) <= 254 for v in contacts)):
        raise ValueError('Invalid contacts metadata')
    return {**data, 'token_endpoint_auth_method':method, 'response_types':responses}


class ExactDCRClient(ProxyDCRClient):
    """Bypass SDK glob/loopback leniency, including '?' in literal queries."""
    def validate_redirect_uri(self, redirect_uri):
        return OAuthClientInformationFull.validate_redirect_uri(self, redirect_uri)
