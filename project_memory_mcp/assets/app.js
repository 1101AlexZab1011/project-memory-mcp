const $ = s => document.querySelector(s);
let current = null, currentArchived = false, offset = 0, busy = false, done = false;
let remotes = [], currentTier = 1, currentPublic = false, currentBorrowed = false;

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
     · tier ${m.tier || 1} · ${esc(m.visibility || 'private')}${m.borrowed ? ' (cached)' : ''}${m.archived_at ? ', archived ' + esc(m.archived_at) : ''}
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
  currentTier = m.tier || 1;
  currentPublic = m.visibility === 'public';
  currentBorrowed = !!m.borrowed;
  publishState();
  $('#detail').showModal();
}

// Publishing is only offered where it means something. A cached copy belongs to
// the server it came from, and a private memory is private because of who it is
// for - so both say why they cannot be published rather than silently omitting
// the control.
function publishState() {
  $('#publishRow').hidden = !remotes.length;
  if (!remotes.length) return;
  $('#visibility').textContent = currentPublic ? 'Make private' : 'Make public';
  $('#visibility').disabled = currentBorrowed;
  const why = currentBorrowed ? 'cached from another server, not yours to publish'
    : !currentPublic ? 'private memories are never published - make it public first'
    : currentTier < 2 ? 'has not earned publication yet; you can publish it anyway'
    : 'publish to the selected server';
  $('#publish').disabled = currentBorrowed || !currentPublic;
  $('#publish').title = why;
}

async function boot() {
  const info = await api('/api/projects');
  $('#project').innerHTML = info.projects.map(p => `<option>${esc(p)}</option>`).join('');
  if (!info.projects.length) { $('#list').innerHTML = '<p class="empty">No projects yet.</p>'; return; }
  await labels(); await loadRemotes(); load(true);
}
async function loadRemotes() {
  // A store with no remotes is a complete setup, not a broken one, so failing
  // to list them must leave the rest of the page working.
  try {
    const data = await api('/api/remotes?project=' + encodeURIComponent($('#project').value));
    remotes = data.remotes || [];
  } catch (e) { remotes = []; }
  $('#remote').innerHTML = remotes.map(r => `<option value="${esc(r.name)}">${esc(r.name)}` +
    `${r.description ? ' - ' + esc(r.description) : ''}</option>`).join('');
}
async function labels() {
  const data = await api('/api/labels?project=' + encodeURIComponent($('#project').value));
  $('#label').innerHTML = '<option value="">any label</option>' +
    data.labels.map(l => `<option>${esc(l)}</option>`).join('');
}

let timer;
$('#q').oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(true), 250); };
$('#status').onchange = $('#label').onchange = () => load(true);
$('#project').onchange = async () => { await labels(); await loadRemotes(); load(true); };
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
$('#visibility').onclick = async () => {
  const next = currentPublic ? 'private' : 'public';
  await api('/api/visibility', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: $('#project').value, id: current, visibility: next})});
  currentPublic = next === 'public';
  publishState();
};
$('#publish').onclick = async () => {
  const remote = $('#remote').value;
  // Tier is the store's evidence that a memory has proven useful. Overriding it
  // is exactly what this button is for - a lesson that is hard-won, rarely
  // needed and expensive to rediscover never accrues the usage to earn a tier -
  // but it should be a deliberate answer to a question, not a side effect.
  if (currentTier < 2 && !confirm(
      `${current} is tier ${currentTier} and has not earned publication.\n\n` +
      `Publish it to ${remote} anyway? Do this for a lesson whose value will never ` +
      `show up in usage: hard-won, rarely needed, expensive to rediscover.`)) return;
  try {
    const r = await api('/api/promote', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project: $('#project').value, id: current, remote,
                            force: currentTier < 2})});
    // Queued is the only answer there is: delivery happens in the background,
    // so promising "published" here would be claiming something unknown.
    alert(`${r.queued} is queued for ${r.remote}. It will be delivered in the ` +
          `background; ${r.waiting} item(s) waiting.`);
  } catch (e) { alert(e.message); }
};
$('#logout').onclick = async () => {
  await api('/api/logout', {method: 'POST'}); location.href = '/login';
};
addEventListener('scroll', () => {
  if (!done && innerHeight + scrollY >= document.body.offsetHeight - 400) load(false);
});
boot();
