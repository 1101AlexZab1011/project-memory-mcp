"""Management UI: browse, search, triage.

Served from the same process and port as the MCP endpoint. Hand-written HTML and
CSS, no framework, no build step, nothing to install - so the zero-dependency
line holds and the page works in any browser on any device.

The script lives in ``assets/app.js`` and is inlined into the page at serve
time. Same single self-contained document over the wire; the difference is that
a file is something a linter, an editor and a test can read, and 175 lines
buried in a Python string literal were none of those. Nothing is fetched
separately, so the no-build promise is unaffected.

Scope is read and triage. Creating and editing memories is the agent's job
through validated tools; what a human needs is to see what is in the store and
retire what should not be.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>project memory</title><style>%(css)s</style></head>
<body class="centered">
  <form class="card login" method="post" action="/api/login">
    <h1>project memory</h1>
    <p class="muted">Enter the server token.</p>
    <input type="password" name="token" placeholder="token" autofocus autocomplete="current-password">
    <button type="submit">Sign in</button>
    %(error)s
  </form>
</body></html>
"""

CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#16181d;--muted:#6b7280;--line:#e5e7eb;
      --card:#fff;--accent:#2563eb;--danger:#dc2626;--chip:#f3f4f6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6e8ee;--muted:#9aa3b2;
      --line:#262b36;--card:#151922;--chip:#1c2230}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body.centered{display:grid;place-items:center;min-height:100vh;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.login{width:min(360px,100%);display:grid;gap:12px}
h1{font-size:18px;margin:0}
input,select,button{font:inherit;padding:10px 12px;border-radius:8px;border:1px solid var(--line);
      background:var(--bg);color:var(--fg);width:100%}
button{background:var(--accent);color:#fff;border-color:transparent;cursor:pointer}
button.ghost{background:transparent;color:var(--fg)}
button.danger{background:var(--danger)}
.muted{color:var(--muted);font-size:13px;margin:0}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
      padding:12px;display:grid;gap:8px;z-index:5}
.row{display:flex;gap:8px;flex-wrap:wrap}
.row>*{flex:1 1 140px}
.row .grow{flex:3 1 220px}
main{padding:12px;display:grid;gap:10px;max-width:900px;margin:0 auto}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer}
.item h2{font-size:15px;margin:0 0 4px;word-break:break-word}
.item p{margin:0;color:var(--muted);font-size:13.5px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.chip{background:var(--chip);border-radius:999px;padding:2px 9px;font-size:12px;color:var(--muted)}
.chip.s-active{color:#15803d}.chip.s-stale{color:#b45309}
.chip.s-wrong{color:var(--danger)}.chip.s-superseded{color:var(--muted)}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--fg);
      padding:0;width:min(720px,94vw);max-height:88vh}
dialog::backdrop{background:#0009}
.detail{padding:16px;overflow:auto;max-height:calc(88vh - 60px)}
.detail h3{margin:16px 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.detail ul{margin:0;padding-left:18px}.detail li{margin:2px 0}
.actions{display:flex;gap:8px;padding:12px;border-top:1px solid var(--line);flex-wrap:wrap}
.actions>*{flex:1 1 120px}
.empty{text-align:center;color:var(--muted);padding:32px 12px}
.err{color:var(--danger);font-size:13px;margin:0}
@media(min-width:700px){main{padding:16px}}
"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>project memory</title><style>%(css)s</style></head>
<body>
<header>
  <div class="row">
    <select id="project" title="project"></select>
    <input id="q" class="grow" type="search" placeholder="search memories">
  </div>
  <div class="row">
    <select id="status">
      <option value="">active + stale</option>
      <option value="all">every status</option>
      <option value="active">active</option><option value="stale">stale</option>
      <option value="superseded">superseded</option><option value="wrong">wrong</option>
      <option value="archived">archived</option>
    </select>
    <select id="label"><option value="">any label</option></select>
    <button class="ghost" id="logout" title="sign out">sign out</button>
  </div>
</header>
<main id="list"></main>
<dialog id="detail"><div class="detail" id="detailBody"></div>
  <div class="actions" id="publishRow" hidden>
    <button class="ghost" id="visibility">Make public</button>
    <select id="remote"></select>
    <button class="ghost" id="publish">Publish</button>
  </div>
  <div class="actions">
    <select id="newStatus">
      <option value="active">active</option><option value="stale">stale</option>
      <option value="superseded">superseded</option><option value="wrong">wrong</option>
    </select>
    <button id="save">Set status</button>
    <button class="ghost" id="archive">Archive</button>
    <button class="danger" id="del">Delete</button>
    <button class="ghost" id="close">Close</button>
  </div>
</dialog>
<script>
%(script)s</script>
</body></html>
"""


def script() -> str:
    """The page script, read from disk.

    Not cached: the file is small, the page is served rarely, and reading it
    every time means editing it during development shows up on the next reload
    without restarting the server.
    """
    return (ASSETS / "app.js").read_text(encoding="utf-8")


def login_page(error: str = "") -> str:
    markup = f'<p class="err">{error}</p>' if error else ""
    return LOGIN_PAGE % {"css": CSS, "error": markup}


def app_page() -> str:
    return PAGE % {"css": CSS, "script": script()}
