"""Presentation only for proxied Pocket ID sign-in documents."""
SIGN_IN_PATHS = frozenset({'/', '/login', '/interaction', '/authorize'})
STYLE = b'''<style id="carpark-pocket-appearance">
body,h1{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif!important}
</style>'''


def style_sign_in(path, content_type, content):
    """Keep Pocket's HTML, nonces, scripts and controls; add only static CSS."""
    if path in SIGN_IN_PATHS and content_type.split(';', 1)[0].strip().lower() == 'text/html':
        return content.replace(b'</head>', STYLE + b'</head>', 1)
    return content
