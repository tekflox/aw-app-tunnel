"""The tunnels editor served into the Settings panel's ``iframe`` widget.

Same reasoning as aw-app-remote-screen's hosts_ui.py: aw-workspace-ui's
declarative renderer has no ``table`` widget and its ``list`` takes static
items from the spec, so a widget spec cannot render a live, editable list of
rows. ``iframe { src: "/api/*" }`` is the vocabulary's own escape hatch, and it
lands on the same origin this page's own fetches go to, so the apex cookie
authorises them and IdentityGuard still applies.

**Layout constraint that drives everything here:** the host renders this in
`.appwin-iframe`, a `min-height: 320px` box inside the Settings sidebar — a
NARROW, SHORT viewport, not a full window. A multi-column table there wraps
every cell into a vertical ribbon (a one-paragraph status turns into a 300px
tall column) and pushes the row actions off the right edge. So: one card per
tunnel, stacked; the long "why is this not running" text collapsed behind a
disclosure instead of inlined; the form single-column with labels above
inputs.

Colours come from the host's own palette (`--color-accent` etc. are defined on
:root by aw-workspace-ui) with rgba fallbacks, so this reads correctly in both
its light and dark themes without shipping a second stylesheet.
"""
from __future__ import annotations

TUNNELS_UI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tunnels</title>
<style>
  :root {
    color-scheme: dark light;
    --accent: var(--color-accent, #f5a623);
    --line: var(--color-border, rgba(128,128,128,.28));
    --muted: var(--color-text-muted, #64748b);
    --panel: rgba(128,128,128,.06);
  }
  * { box-sizing: border-box; }
  /* The gutter has to live HERE. The host renders this page in a
     cross-origin iframe, so no stylesheet of its can reach inside; padding on
     the <iframe> itself only shifts the origin and clips the right-hand side
     (tried, reverted). Without this the form sits flush against the frame. */
  body { margin: 0; padding: 12px; font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: transparent; color: inherit; }

  /* ── cards ─────────────────────────────────────────────────────────── */
  .card { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
          margin-bottom: 8px; background: var(--panel); }
  .card-top { display: flex; align-items: center; gap: 8px; }
  .name { font-weight: 600; font-size: 13px; flex: 1; min-width: 0;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .route { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
           font-size: 11px; color: var(--muted); margin-top: 4px;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 11px; color: var(--muted); margin-top: 4px; }

  .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
  .dot.up { background: #4ade80; box-shadow: 0 0 0 3px rgba(74,222,128,.15); }
  .dot.down { background: #94a3b8; }
  .dot.err { background: #f87171; box-shadow: 0 0 0 3px rgba(248,113,113,.15); }
  .state { font-size: 11px; color: var(--muted); flex: none; }

  /* The not-ready explanation is a paragraph. Inlining it in a narrow pane is
     what made the old table unreadable — keep it one line, expandable. */
  details.why { margin-top: 6px; }
  details.why summary { cursor: pointer; font-size: 11px; color: var(--muted);
                        list-style: none; display: flex; align-items: center; gap: 5px; }
  details.why summary::-webkit-details-marker { display: none; }
  details.why summary::before { content: "▸"; font-size: 9px; transition: transform .15s; }
  details.why[open] summary::before { transform: rotate(90deg); }
  details.why p { margin: 6px 0 0; font-size: 11px; line-height: 1.5; color: var(--muted);
                  padding-left: 14px; }

  /* ── buttons ───────────────────────────────────────────────────────── */
  .actions { display: flex; gap: 5px; flex: none; }
  button { font: inherit; font-size: 11px; font-weight: 500; padding: 4px 10px;
           border-radius: 6px; border: 1px solid var(--line);
           background: transparent; color: inherit; cursor: pointer;
           transition: background .12s, border-color .12s, color .12s; }
  button:hover { background: rgba(128,128,128,.16); border-color: rgba(128,128,128,.45); }
  button.primary { background: var(--accent); border-color: var(--accent);
                   color: #1a1205; font-weight: 600; }
  button.primary:hover { filter: brightness(1.08); background: var(--accent); }
  button.ghost { border-color: transparent; color: var(--muted); padding: 4px 7px; }
  button.ghost:hover { color: inherit; }
  button.danger:hover { background: rgba(248,113,113,.14);
                        border-color: rgba(248,113,113,.45); color: #f87171; }

  /* ── form ──────────────────────────────────────────────────────────── */
  h4 { margin: 18px 0 8px; font-size: 11px; text-transform: uppercase;
       letter-spacing: .05em; color: var(--muted); font-weight: 600; }
  .field { margin-bottom: 10px; }
  .field > label { display: block; font-size: 12px; font-weight: 500; margin-bottom: 4px; }
  input, select { font: inherit; font-size: 12px; padding: 6px 8px; border-radius: 6px;
                  border: 1px solid var(--line); background: rgba(128,128,128,.08);
                  color: inherit; width: 100%; }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  input[type=checkbox] { width: auto; accent-color: var(--accent); }
  .hint { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.45; }
  .row2 { display: flex; gap: 8px; }
  .row2 > .field { flex: 1; margin-bottom: 0; }
  .form-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
  .check { display: flex; align-items: center; gap: 8px; }

  .msg { padding: 7px 10px; border-radius: 7px; font-size: 12px; margin-bottom: 10px;
         line-height: 1.45; }
  .msg.err { background: rgba(248,113,113,.13); color: #fca5a5; }
  .msg.ok  { background: rgba(74,222,128,.13); color: #86efac; }
  .empty { color: var(--muted); font-size: 12px; padding: 14px 0; text-align: center; }
</style>
</head>
<body>
<div id="msg"></div>
<div id="list"></div>

<h4 id="form-title">Add a tunnel</h4>
<form id="form" autocomplete="off">
  <input type="hidden" id="id">

  <div class="field">
    <label for="name">Name</label>
    <input id="name" placeholder="macbook-vnc" required>
  </div>

  <div class="row2">
    <div class="field">
      <label for="listen_port">Local port</label>
      <input id="listen_port" type="number" min="1" max="65535" placeholder="15900" required>
    </div>
    <div class="field">
      <label for="dest_port">Destination port</label>
      <input id="dest_port" type="number" min="1" max="65535" placeholder="5900" required>
    </div>
  </div>

  <div class="field">
    <label for="listen_host">Who can use it</label>
    <select id="listen_host">
      <option value="127.0.0.1">This workspace only (recommended)</option>
      <option value="0.0.0.0">Also other app containers</option>
    </select>
    <div class="hint">Apps running inside the workspace share its loopback, so the first
      option already covers them. The second also reaches app containers with their own
      network &mdash; at the cost of every one of them being able to use this tunnel.</div>
  </div>

  <div class="field">
    <label for="dest_kind">Destination</label>
    <select id="dest_kind">
      <option value="custom">Custom IP / host (TCP proxy)</option>
      <option value="remote_host">Remote host (over its /link tunnel)</option>
    </select>
  </div>

  <div class="field" id="rh-field">
    <label for="remote_host_id">Remote host</label>
    <select id="remote_host_id"></select>
    <div class="hint" id="rh-hint"></div>
  </div>

  <div class="field">
    <label for="dest_host" id="dest-host-label">Destination host</label>
    <input id="dest_host" placeholder="127.0.0.1">
  </div>

  <div class="field check">
    <input id="enabled" type="checkbox" checked>
    <label for="enabled" style="margin:0">Enabled</label>
  </div>

  <div class="form-actions">
    <button type="button" id="cancel" hidden>Cancel</button>
    <button type="submit" class="primary" id="save">Add tunnel</button>
  </div>
</form>

<script>
const BASE = '/api/apps/tunnel';
const $ = (id) => document.getElementById(id);
let tunnels = [];
let hosts = [];

async function call(method, path, body) {
  const init = { method, credentials: 'include' };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, init);
  let payload = {};
  try { payload = await res.json(); } catch (_e) {}
  if (!res.ok) throw new Error(payload.detail || ('HTTP ' + res.status));
  return payload;
}

function say(text, kind) {
  $('msg').innerHTML = text ? '<div class="msg ' + kind + '">' + text + '</div>' : '';
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function bytes(n) {
  if (!n) return '0';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}

function stateOf(t) {
  if (t.listening) return ['up', 'listening'];
  if (t.not_ready_reason) return ['err', 'not ready'];
  if (t.last_error) return ['err', 'error'];
  if (!t.enabled) return ['down', 'disabled'];
  return ['down', 'stopped'];
}

function destOf(t) {
  if (t.dest_kind === 'remote_host') {
    const h = hosts.find((x) => x.id === t.remote_host_id);
    return (h ? h.hostname : (t.remote_host_id || '?')) + ' \\u2192 ' + t.dest_host + ':' + t.dest_port;
  }
  return t.dest_host + ':' + t.dest_port;
}

function render() {
  if (!tunnels.length) {
    $('list').innerHTML = '<div class="empty">No tunnels yet.</div>';
    return;
  }
  $('list').innerHTML = tunnels.map((t) => {
    const [cls, label] = stateOf(t);
    const why = t.not_ready_reason || t.last_error;
    const traffic = t.total_connections
      ? t.active_connections + ' active \\u00b7 ' + t.total_connections + ' total \\u00b7 \\u2191'
        + bytes(t.bytes_up) + ' \\u2193' + bytes(t.bytes_down)
      : '';
    return '<div class="card">'
      + '<div class="card-top">'
      +   '<span class="dot ' + cls + '"></span>'
      +   '<span class="name" title="' + esc(t.name) + '">' + esc(t.name) + '</span>'
      +   '<span class="state">' + label + '</span>'
      +   '<span class="actions">'
      +     '<button class="primary" data-start="' + esc(t.id) + '">'
      +       (t.listening ? 'Restart' : 'Start') + '</button>'
      +     (t.listening ? '<button data-stop="' + esc(t.id) + '">Stop</button>' : '')
      +     '<button class="ghost" data-edit="' + esc(t.id) + '">Edit</button>'
      +     '<button class="ghost danger" data-del="' + esc(t.id) + '">Delete</button>'
      +   '</span>'
      + '</div>'
      + '<div class="route" title="' + esc(t.listen_host + ':' + t.listen_port + ' \\u2192 ' + destOf(t)) + '">'
      +   esc(t.listen_host) + ':' + t.listen_port + ' \\u2192 ' + esc(destOf(t)) + '</div>'
      + (traffic ? '<div class="meta">' + traffic + '</div>' : '')
      + (why ? '<details class="why"><summary>Why is it not running?</summary>'
             + '<p>' + esc(why) + '</p></details>' : '')
      + '</div>';
  }).join('');
}

async function refresh() {
  tunnels = (await call('GET', '/tunnels')).tunnels || [];
  render();
}

async function loadHosts() {
  let payload = { hosts: [], error: 'not loaded' };
  try { payload = await call('GET', '/remote-hosts'); } catch (e) { payload.error = e.message; }
  hosts = payload.hosts || [];
  $('remote_host_id').innerHTML = hosts.length
    ? hosts.map((h) => '<option value="' + esc(h.id) + '">' + esc(h.hostname) +
        ' (' + esc(h.os) + ', ' + (h.connected ? 'online' : 'offline') + ')</option>').join('')
    : '<option value="">no linked hosts found</option>';
  $('rh-hint').textContent = payload.error
    ? 'Could not list linked hosts: ' + payload.error
    : 'The destination host:port is resolved ON that machine, not from here.';
}

function applyKind() {
  const remote = $('dest_kind').value === 'remote_host';
  $('rh-field').hidden = !remote;
  $('dest-host-label').textContent = remote ? 'Host (as seen there)' : 'Destination host';
}
$('dest_kind').addEventListener('change', applyKind);

function resetForm() {
  $('id').value = '';
  $('form').reset();
  $('enabled').checked = true;
  $('form-title').textContent = 'Add a tunnel';
  $('save').textContent = 'Add tunnel';
  $('cancel').hidden = true;
  applyKind();
}

function loadForEdit(id) {
  const t = tunnels.find((x) => x.id === id);
  if (!t) return;
  $('id').value = t.id;
  $('name').value = t.name;
  $('listen_port').value = t.listen_port;
  $('listen_host').value = t.listen_host;
  $('dest_kind').value = t.dest_kind;
  $('dest_host').value = t.dest_host;
  $('dest_port').value = t.dest_port;
  $('enabled').checked = t.enabled;
  if (t.remote_host_id) $('remote_host_id').value = t.remote_host_id;
  $('form-title').textContent = 'Edit ' + t.name;
  $('save').textContent = 'Save changes';
  $('cancel').hidden = false;
  applyKind();
  $('name').focus();
  $('form-title').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('list').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  const id = b.getAttribute('data-start') || b.getAttribute('data-stop') ||
             b.getAttribute('data-edit') || b.getAttribute('data-del');
  if (!id) return;
  try {
    if (b.hasAttribute('data-edit')) return loadForEdit(id);
    if (b.hasAttribute('data-start')) {
      const r = await call('POST', '/tunnels/' + encodeURIComponent(id) + '/start');
      say(r.started ? 'Started.' : esc(r.reason || 'Not started.'), r.started ? 'ok' : 'err');
    } else if (b.hasAttribute('data-stop')) {
      await call('POST', '/tunnels/' + encodeURIComponent(id) + '/stop');
      say('Stopped.', 'ok');
    } else {
      const t = tunnels.find((x) => x.id === id);
      if (!confirm('Delete "' + (t ? t.name : id) + '"?')) return;
      await call('DELETE', '/tunnels/' + encodeURIComponent(id));
      if ($('id').value === id) resetForm();
      say('Deleted.', 'ok');
    }
    await refresh();
  } catch (err) { say(esc(err.message), 'err'); }
});

$('cancel').addEventListener('click', () => { resetForm(); say('', 'ok'); });

$('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = $('id').value;
  const body = {
    name: $('name').value.trim(),
    listen_port: Number($('listen_port').value),
    listen_host: $('listen_host').value,
    dest_kind: $('dest_kind').value,
    dest_host: $('dest_host').value.trim(),
    dest_port: Number($('dest_port').value),
    enabled: $('enabled').checked,
  };
  if (body.dest_kind === 'remote_host') body.remote_host_id = $('remote_host_id').value;
  try {
    const saved = id
      ? await call('PUT', '/tunnels/' + encodeURIComponent(id), body)
      : await call('POST', '/tunnels', body);
    resetForm();
    await refresh();
    say(saved.not_ready_reason
      ? 'Saved, but not started \\u2014 see the tunnel above.'
      : (id ? 'Saved.' : 'Tunnel added.'), saved.not_ready_reason ? 'err' : 'ok');
  } catch (err) { say(esc(err.message), 'err'); }
});

// Live counters are the point of this page — a tunnel you just created should
// visibly start carrying bytes without a manual reload. Skip the refresh while
// a disclosure is open or a field is focused, so it never yanks the UI.
setInterval(() => {
  if (document.querySelector('details.why[open]')) return;
  if (['INPUT', 'SELECT'].includes(document.activeElement?.tagName)) return;
  refresh().catch(() => {});
}, 4000);

applyKind();
loadHosts();
refresh().catch((err) => say('Could not load tunnels: ' + esc(err.message), 'err'));
</script>
</body>
</html>
"""
