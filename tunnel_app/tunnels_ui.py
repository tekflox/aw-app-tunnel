"""The tunnels editor served into the Settings panel's ``iframe`` widget.

Same reasoning as aw-app-remote-screen's hosts_ui.py: aw-workspace-ui's
declarative renderer has no ``table`` widget and its ``list`` takes static items
from the spec, so a widget spec cannot render a live, editable list of rows.
``iframe { src: "/api/*" }`` is the vocabulary's own escape hatch, and it lands
on the same origin this page's own fetches go to, so the apex cookie authorises
them and IdentityGuard still applies.
"""
from __future__ import annotations

TUNNELS_UI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tunnels</title>
<style>
  :root { color-scheme: dark light; }
  body { margin: 0; font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: transparent; color: inherit; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(128,128,128,.25);
           font-size: 12px; vertical-align: top; }
  th { font-weight: 600; opacity: .7; font-size: 11px; text-transform: uppercase;
       letter-spacing: .04em; }
  td.addr { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .actions { text-align: right; white-space: nowrap; }
  button { font: inherit; font-size: 11px; padding: 3px 9px; border-radius: 5px;
           border: 1px solid rgba(128,128,128,.35); background: rgba(128,128,128,.12);
           color: inherit; cursor: pointer; }
  button:hover { background: rgba(128,128,128,.22); }
  button.danger { border-color: rgba(220,80,80,.4); color: #e88; }
  button.primary { border-color: rgba(90,150,240,.5); color: #8ab4f8; }
  form { display: grid; grid-template-columns: 150px 1fr; gap: 8px 10px; align-items: center; }
  form label { font-size: 12px; opacity: .8; }
  input, select { font: inherit; font-size: 12px; padding: 5px 7px; border-radius: 5px;
                  border: 1px solid rgba(128,128,128,.35); background: rgba(128,128,128,.1);
                  color: inherit; width: 100%; box-sizing: border-box; }
  .row-span { grid-column: 1 / -1; display: flex; gap: 8px; justify-content: flex-end; }
  .hint { grid-column: 1 / -1; font-size: 11px; opacity: .6; margin: -2px 0 4px; }
  .msg { padding: 6px 8px; border-radius: 5px; font-size: 12px; margin-bottom: 10px; }
  .msg.err { background: rgba(220,80,80,.15); color: #f2a0a0; }
  .msg.ok  { background: rgba(80,190,120,.15); color: #9edeb0; }
  .empty { opacity: .6; font-size: 12px; padding: 10px 0; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 5px; }
  .up { background: #5ac37d; } .down { background: #777; } .err { background: #e07070; }
  .why { font-size: 11px; opacity: .65; margin-top: 3px; }
  h4 { margin: 16px 0 4px; font-size: 12px; text-transform: uppercase;
       letter-spacing: .04em; opacity: .7; }
</style>
</head>
<body>
<div id="msg"></div>
<div id="list"></div>
<h4 id="form-title">Add a tunnel</h4>
<form id="form" autocomplete="off">
  <input type="hidden" id="id">
  <label for="name">Name</label>
  <input id="name" placeholder="macbook-vnc" required>

  <label for="listen_port">Local port</label>
  <input id="listen_port" type="number" min="1" max="65535" placeholder="15900" required>

  <label for="listen_host">Who can use it</label>
  <select id="listen_host">
    <option value="127.0.0.1">This workspace only (recommended)</option>
    <option value="0.0.0.0">Also other app containers</option>
  </select>
  <div class="hint">Tier-1 apps (Remote Screen among them) run inside the workspace and share
    its loopback, so the first option already covers them. The second also reaches Tier-2
    containers (Browser, Code Server), which have their own network &mdash; at the cost of
    every container on that network being able to use this tunnel.</div>

  <label for="dest_kind">Destination</label>
  <select id="dest_kind">
    <option value="custom">Custom IP / host (TCP proxy)</option>
    <option value="remote_host">Remote host (over its /link tunnel)</option>
  </select>

  <label for="remote_host_id" id="rh-label">Remote host</label>
  <select id="remote_host_id"></select>
  <div class="hint" id="rh-hint"></div>

  <label for="dest_host" id="dest-host-label">Destination host</label>
  <input id="dest_host" placeholder="127.0.0.1">
  <label for="dest_port">Destination port</label>
  <input id="dest_port" type="number" min="1" max="65535" placeholder="5900" required>

  <label for="enabled">Enabled</label>
  <input id="enabled" type="checkbox" checked style="width:auto">

  <div class="row-span">
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

function stateDot(t) {
  if (t.listening) return '<span class="dot up"></span>listening';
  if (t.last_error) return '<span class="dot err"></span>error';
  return '<span class="dot down"></span>stopped';
}

function destLabel(t) {
  if (t.dest_kind === 'remote_host') {
    const h = hosts.find((x) => x.id === t.remote_host_id);
    return (h ? esc(h.hostname) : esc(t.remote_host_id || '?')) +
           ' &rarr; ' + esc(t.dest_host) + ':' + t.dest_port;
  }
  return esc(t.dest_host) + ':' + t.dest_port;
}

function render() {
  if (!tunnels.length) {
    $('list').innerHTML = '<div class="empty">No tunnels yet.</div>';
    return;
  }
  $('list').innerHTML =
    '<table><thead><tr><th>Name</th><th>Listens on</th><th>Forwards to</th>' +
    '<th>State</th><th>Traffic</th><th></th></tr></thead><tbody>' +
    tunnels.map((t) =>
      '<tr><td>' + esc(t.name) + (t.enabled ? '' : ' <span class="why">(disabled)</span>') + '</td>' +
      '<td class="addr">' + esc(t.listen_host) + ':' + t.listen_port + '</td>' +
      '<td class="addr">' + destLabel(t) + '</td>' +
      '<td>' + stateDot(t) +
        (t.not_ready_reason ? '<div class="why">' + esc(t.not_ready_reason) + '</div>' : '') +
        (t.last_error && !t.not_ready_reason
          ? '<div class="why">' + esc(t.last_error) + '</div>' : '') + '</td>' +
      '<td>' + t.active_connections + ' active / ' + t.total_connections + ' total' +
        '<div class="why">&uarr; ' + bytes(t.bytes_up) + ' &darr; ' + bytes(t.bytes_down) + '</div></td>' +
      '<td class="actions">' +
        '<button data-start="' + esc(t.id) + '">' + (t.listening ? 'Restart' : 'Start') + '</button> ' +
        (t.listening ? '<button data-stop="' + esc(t.id) + '">Stop</button> ' : '') +
        '<button data-edit="' + esc(t.id) + '">Edit</button> ' +
        '<button class="danger" data-del="' + esc(t.id) + '">Delete</button>' +
      '</td></tr>'
    ).join('') + '</tbody></table>';
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
  for (const id of ['remote_host_id']) {
    $(id).hidden = !remote;
    const l = document.querySelector('label[for="' + id + '"]');
    if (l) l.hidden = !remote;
  }
  $('rh-hint').hidden = !remote;
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
}

$('list').addEventListener('click', async (e) => {
  const t = e.target;
  const id = t.getAttribute('data-start') || t.getAttribute('data-stop') ||
             t.getAttribute('data-edit') || t.getAttribute('data-del');
  if (!id) return;
  try {
    if (t.hasAttribute('data-edit')) return loadForEdit(id);
    if (t.hasAttribute('data-start')) {
      const r = await call('POST', '/tunnels/' + encodeURIComponent(id) + '/start');
      say(r.started ? 'Started.' : esc(r.reason || 'Not started.'), r.started ? 'ok' : 'err');
    } else if (t.hasAttribute('data-stop')) {
      await call('POST', '/tunnels/' + encodeURIComponent(id) + '/stop');
      say('Stopped.', 'ok');
    } else {
      const tun = tunnels.find((x) => x.id === id);
      if (!confirm('Delete "' + (tun ? tun.name : id) + '"?')) return;
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
      ? 'Saved, but not started: ' + esc(saved.not_ready_reason)
      : (id ? 'Saved.' : 'Tunnel added.'), saved.not_ready_reason ? 'err' : 'ok');
  } catch (err) { say(esc(err.message), 'err'); }
});

// Live counters are the whole point of this page — a tunnel you just created
// should visibly start carrying bytes without a manual reload.
setInterval(() => refresh().catch(() => {}), 4000);

applyKind();
loadHosts();
refresh().catch((err) => say('Could not load tunnels: ' + esc(err.message), 'err'));
</script>
</body>
</html>
"""
