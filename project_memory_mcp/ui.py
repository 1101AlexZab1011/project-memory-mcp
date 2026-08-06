"""Management UI: browse, search, triage.

Served from the same process and port as the MCP endpoint. Hand-written HTML,
CSS and JS as static strings - no framework, no build step, nothing to install
- so the zero-dependency line holds and the page works in any browser on any
device.

Scope is read and triage. Creating and editing memories is the agent's job
through validated tools; what a human needs is to see what is in the store and
retire what should not be.
"""

from __future__ import annotations

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
const $ = s => document.querySelector(s);
let current = null, currentArchived = false, offset = 0, busy = false, done = false;

async function api(path, options) {
  const r = await fetch(path, Object.assign({credentials: 'same-origin'}, options || {}));
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  if (!r.ok) throw new Error((await r.json()).error || r.statusText);
  return r.json();
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function params(extra) {
  const p = new URLSearchParams({project: $('#project').value, ...extra});
  if ($('#q').value.trim()) p.set('q', $('#q').value.trim());
  if ($('#status').value) p.set('status', $('#status').value);
  if ($('#label').value) p.set('label', $('#label').value);
  return p;
}

function card(m) {
  const el = document.createElement('article');
  el.className = 'item';
  const used = m.usage || {};
  el.innerHTML = `<h2>${esc(m.id)}</h2><p>${esc(m.description)}</p><div class="chips">
    <span class="chip s-${esc(m.status)}">${esc(m.status)}</span>
    ${m.archived_at ? '<span class="chip s-superseded">archived</span>' : ''}
    ${(m.labels||[]).slice(0,4).map(l=>`<span class="chip">${esc(l)}</span>`).join('')}
    <span class="chip">shown ${used.surfaced||0} / used ${used.applied||0}</span></div>`;
  el.onclick = () => open(m.id);
  return el;
}

async function load(reset) {
  if (busy) return;
  busy = true;
  if (reset) { offset = 0; done = false; $('#list').innerHTML = ''; }
  try {
    const data = await api('/api/memories?' + params({offset, limit: 25}));
    if (!data.memories.length && !offset)
      $('#list').innerHTML = '<p class="empty">Nothing matches.</p>';
    data.memories.forEach(m => $('#list').append(card(m)));
    offset += data.memories.length;
    done = data.memories.length < 25;
  } catch (e) { $('#list').innerHTML = `<p class="empty err">${esc(e.message)}</p>`; }
  busy = false;
}

function section(title, items) {
  if (!items || !items.length) return '';
  return `<h3>${title}</h3><ul>${items.map(i =>
    `<li>${esc(typeof i === 'string' ? i : i.id + ' — ' + i.reason)}</li>`).join('')}</ul>`;
}

async function open(id) {
  const m = await api(`/api/memory?project=${encodeURIComponent($('#project').value)}&id=${encodeURIComponent(id)}`);
  current = id;
  const u = m.usage || {}, r = m.memory.relationships || {}, s = m.memory.scope || {};
  $('#detailBody').innerHTML = `<h1>${esc(m.memory.id)}</h1>
    <p class="muted">${esc(m.memory.status)} · created ${esc((m.memory.evidence||{}).created || '—')}
     · tier ${m.tier || 1}${m.archived_at ? ', archived ' + esc(m.archived_at) : ''}
     · shown ${u.surfaced||0} (${u.surfaced_direct||0} direct), used ${u.applied||0}
     · ${u.spread_days||0} distinct days</p>
    <p>${esc(m.memory.description)}</p>
    ${section('labels', m.memory.labels)}${section('tags', m.memory.tags)}
    ${section('triggers', m.memory.triggers)}${section('facts', m.memory.remembered_facts)}
    ${section('solution', m.memory.solution_pattern)}${section('pitfalls', m.memory.pitfalls)}
    ${section('files', s.files)}${section('related', r.related)}
    ${section('supersedes', r.supersedes)}${section('superseded by', r.superseded_by)}`;
  $('#newStatus').value = m.memory.status;
  currentArchived = !!m.archived_at;
  $('#archive').textContent = currentArchived ? 'Restore' : 'Archive';
  $('#detail').showModal();
}

async function boot() {
  const info = await api('/api/projects');
  $('#project').innerHTML = info.projects.map(p => `<option>${esc(p)}</option>`).join('');
  if (!info.projects.length) { $('#list').innerHTML = '<p class="empty">No projects yet.</p>'; return; }
  await labels(); load(true);
}
async function labels() {
  const data = await api('/api/labels?project=' + encodeURIComponent($('#project').value));
  $('#label').innerHTML = '<option value="">any label</option>' +
    data.labels.map(l => `<option>${esc(l)}</option>`).join('');
}

let timer;
$('#q').oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(true), 250); };
$('#status').onchange = $('#label').onchange = () => load(true);
$('#project').onchange = async () => { await labels(); load(true); };
$('#close').onclick = () => $('#detail').close();
$('#save').onclick = async () => {
  await api('/api/status', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: $('#project').value, id: current, status: $('#newStatus').value})});
  $('#detail').close(); load(true);
};
$('#archive').onclick = async () => {
  await api('/api/archive', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: $('#project').value, id: current, archived: !currentArchived})});
  $('#detail').close(); load(true);
};
$('#del').onclick = async () => {
  if (!confirm(`Delete ${current}? References to it are cleaned up, but this cannot be undone.`)) return;
  await api('/api/delete', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: $('#project').value, id: current})});
  $('#detail').close(); load(true);
};
$('#logout').onclick = async () => {
  await api('/api/logout', {method: 'POST'}); location.href = '/login';
};
addEventListener('scroll', () => {
  if (!done && innerHeight + scrollY >= document.body.offsetHeight - 400) load(false);
});
boot();
</script>
</body></html>
"""


def login_page(error: str = "") -> str:
    markup = f'<p class="err">{error}</p>' if error else ""
    return LOGIN_PAGE % {"css": CSS, "error": markup}


def app_page() -> str:
    return PAGE % {"css": CSS}
