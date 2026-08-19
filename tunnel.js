
const BASE = '/api/apps/tunnel';
const $ = (id) => document.getElementById(id);
let tunnels = [];
// Id of the tunnel whose Delete is armed. This panel CANNOT use confirm(): the
// host renders it in a sandbox without allow-modals (aw-workspace-ui
// AppWindow.jsx — "allow-scripts allow-forms allow-same-origin"), so the
// browser ignores the call and returns false. `if (!confirm(...)) return;` was
// therefore an unconditional return and Delete silently did nothing.
let armed = null;
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
    return (h ? h.hostname : (t.remote_host_id || '?')) + ' \u2192 ' + t.dest_host + ':' + t.dest_port;
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
      ? t.active_connections + ' active \u00b7 ' + t.total_connections + ' total \u00b7 \u2191'
        + bytes(t.bytes_up) + ' \u2193' + bytes(t.bytes_down)
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
      +     (armed === t.id
                ? '<button class="ghost danger armed" data-confirm-del="' + esc(t.id) + '">Confirm</button>'
                  + '<button class="ghost" data-cancel-del="1">Cancel</button>'
                : '<button class="ghost danger" data-del="' + esc(t.id) + '">Delete</button>')
      +   '</span>'
      + '</div>'
      + '<div class="route" title="' + esc(t.listen_host + ':' + t.listen_port + ' \u2192 ' + destOf(t)) + '">'
      +   esc(t.listen_host) + ':' + t.listen_port + ' \u2192 ' + esc(destOf(t)) + '</div>'
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
  if (b.hasAttribute('data-cancel-del')) { armed = null; return render(); }
  const id = b.getAttribute('data-start') || b.getAttribute('data-stop') ||
             b.getAttribute('data-edit') || b.getAttribute('data-del') ||
             b.getAttribute('data-confirm-del');
  if (!id) return;
  try {
    if (b.hasAttribute('data-edit')) return loadForEdit(id);
    if (b.hasAttribute('data-start')) {
      const r = await call('POST', '/tunnels/' + encodeURIComponent(id) + '/start');
      say(r.started ? 'Started.' : esc(r.reason || 'Not started.'), r.started ? 'ok' : 'err');
    } else if (b.hasAttribute('data-stop')) {
      await call('POST', '/tunnels/' + encodeURIComponent(id) + '/stop');
      say('Stopped.', 'ok');
    } else if (b.hasAttribute('data-del')) {
      const t = tunnels.find((x) => x.id === id);
      armed = id;
      render();
      say('Delete "' + esc(t ? t.name : id) + '"? Click Confirm.', 'err');
      return;
    } else {
      armed = null;
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
      ? 'Saved, but not started \u2014 see the tunnel above.'
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
