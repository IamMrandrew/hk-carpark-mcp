"""Carpark consent presentation; FastMCP owns all transaction and CSRF handling."""
from html import escape
from urllib.parse import urlsplit

from hkparking.deployment import SETTINGS

STYLE = """
:root{color-scheme:light dark;font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;
--bg:#fff;--fg:#1d1d1f;--secondary:#636366;--line:#e5e5ea;--soft:#f5f5f7;
--button:#1d1d1f;--button-text:#fff;background:var(--soft);color:var(--fg);-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}body{margin:0;padding:48px 20px}.content{max-width:400px;margin:auto;
padding:40px 28px 24px;background:var(--bg);border:1px solid var(--line);border-radius:24px}
.connection{display:grid;grid-template-columns:56px 20px 56px;grid-template-rows:56px auto;
column-gap:16px;row-gap:8px;align-items:center;justify-items:center;width:max-content;
margin:0 auto 28px}.app{display:contents}.icon{width:56px;height:56px;
display:grid;place-items:center;background:var(--soft);border-radius:15px;font-size:30px;font-weight:600;
grid-row:1}.app:first-child .icon,.app:first-child span{grid-column:1}
.app:last-child .icon,.app:last-child span{grid-column:3}.app span{grid-row:2;align-self:start;
font-size:12px;font-weight:500;color:var(--secondary);text-align:center;line-height:1.4;
max-width:100%;overflow-wrap:anywhere}
.arrow{grid-column:2;grid-row:1;display:grid;place-items:center;height:56px;font-size:22px;
line-height:1;color:var(--secondary)}h1{font-size:27px;font-weight:600;letter-spacing:-.8px;
line-height:1.18;text-align:left;margin:0 0 14px;overflow-wrap:anywhere}
.description{font-size:15px;line-height:1.5;color:var(--secondary);margin:0 0 30px}
.row{display:flex;justify-content:space-between;align-items:center;font-size:13px;min-height:46px;
border-bottom:1px solid var(--line)}.muted{color:var(--secondary)}
details{border-bottom:1px solid var(--line)}summary{display:flex;align-items:center;
justify-content:space-between;gap:8px;min-height:48px;list-style:none;font-size:13px;cursor:pointer}
summary::-webkit-details-marker{display:none}.right{display:flex;align-items:center;gap:8px;
color:var(--secondary);font-size:12px;min-width:0}.host{overflow-wrap:anywhere}
.chevron{height:6px;width:6px;flex-shrink:0;border-right:1px solid currentColor;
border-bottom:1px solid currentColor;transform:rotate(-45deg);margin-right:2px}
details[open] .chevron{transform:rotate(45deg)}.technical{padding:0 0 16px;font-size:12px;
line-height:1.6;color:var(--secondary)}code{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:11px;overflow-wrap:anywhere;margin:7px 0 12px}.actions{margin-top:30px}
button{font:600 15px -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;cursor:pointer;
min-height:48px;width:100%;border:0;border-radius:12px;color:var(--button-text);background:var(--button)}
button.secondary{color:var(--secondary);background:transparent;margin-top:6px;font-weight:400}
button:focus-visible,summary:focus-visible{outline:2px solid var(--fg);outline-offset:4px}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f5f5f7;--secondary:#a1a1a6;
--line:#38383a;--soft:#2c2c2e;--button:#f5f5f7;--button-text:#1c1c1e}}
@media(max-width:440px){body{padding:24px 16px}.content{padding:32px 24px 20px}}
"""


def render_consent(*, client_name, client_id, redirect_uri, scopes, txn_id, csrf_token):
    """Escape request-derived labels; submit to the unchanged same-origin handler."""
    name = client_name or client_id
    safe_name = escape(name)
    initial = escape(name[:1].upper())
    host = escape(urlsplit(redirect_uri).netloc)
    permission = escape(', '.join(scopes))
    display_name = escape(SETTINGS.display_name)
    short_name = escape(SETTINGS.short_name)
    server_initial = escape(SETTINGS.icon_text)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>Connect to {display_name}</title><style>{STYLE}</style></head><body><main class="content">
<div class="connection" aria-label="{safe_name} to {display_name}">
<div class="app"><div class="icon" aria-hidden="true">{initial}</div><span>{safe_name}</span></div>
<span class="arrow" aria-hidden="true">→</span>
<div class="app"><div class="icon" aria-hidden="true">{server_initial}</div><span>{short_name}</span></div></div>
<h1>Connect {safe_name}<br>to {display_name}?</h1>
<p class="description">Find car parks and check live vacancies.</p>
<div class="row"><span>Access</span><span class="muted">Read-only</span></div>
<details><summary><span>Connection details</span><span class="right"><span class="host">{host}</span>
<i class="chevron" aria-hidden="true"></i></span></summary>
<div class="technical">Application ID:<code>{escape(client_id)}</code>
Permission:<code>{permission}</code>Return address:<code>{escape(redirect_uri)}</code></div></details>
<form id="consentForm" method="POST" action="">
<input type="hidden" name="txn_id" value="{escape(txn_id, quote=True)}">
<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">
<input type="hidden" name="submit" value="true">
<div class="actions"><button type="submit" name="action" value="approve">Allow</button>
<button type="submit" name="action" value="deny" class="secondary">Cancel</button></div>
</form></main></body></html>'''
