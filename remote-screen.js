
const BASE = '/api/apps/remote-screen';
const $ = (id) => document.getElementById(id);
const ANDROID_ONLY = ['device_serial', 'adb_bin', 'agent_base_url', 'agent_kind'];
let hosts = [];
// Id of the host whose Delete is armed. This panel CANNOT use confirm(): the
// host renders it in a sandbox without allow-modals (aw-workspace-ui
// AppWindow.jsx — "allow-scripts allow-forms allow-same-origin"), so the
// browser ignores the call and returns false. `if (!confirm(...)) return;`
// was therefore an unconditional return and Delete silently did nothing.
let armed = null;

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

function addrOf(h) {
  if (h.protocol === 'android') {
    return h.host + (h.device_serial ? ' \u00b7 ' + h.device_serial : ' \u00b7 default device');
  }
  return h.host + ':' + h.port;
}

function render() {
  if (!hosts.length) {
    $('list').innerHTML = '<div class="empty">No hosts yet.</div>';
    return;
  }
  $('list').innerHTML = hosts.map((h) =>
    '<div class="card">'
    + '<div class="card-top">'
    +   '<span class="name" title="' + esc(h.name) + '">' + esc(h.name) + '</span>'
    +   '<span class="tag' + (h.supported ? '' : ' warn') + '">'
    +     esc(h.protocol.toUpperCase()) + '</span>'
    +   '<span class="actions">'
    +     '<button class="ghost" data-edit="' + esc(h.id) + '">Edit</button>'
    +     (armed === h.id
              ? '<button class="ghost danger armed" data-confirm-del="' + esc(h.id) + '">Confirm</button>'
                + '<button class="ghost" data-cancel-del="1">Cancel</button>'
              : '<button class="ghost danger" data-del="' + esc(h.id) + '">Delete</button>')
    +   '</span>'
    + '</div>'
    + '<div class="addr" title="' + esc(addrOf(h)) + '">' + esc(addrOf(h)) + '</div>'
    + (h.supported
        ? (h.has_password ? '<div class="note">Password saved</div>' : '')
        : '<div class="note">No browser client for this protocol yet &mdash; '
          + 'stored, not connectable.</div>')
    + '</div>').join('');
}

async function refresh() {
  hosts = (await call('GET', '/hosts')).hosts || [];
  render();
}

// A form that shows "Port" for an Android device and "adb path" for a VNC box
// teaches the wrong model of what each protocol needs, so swap the halves
// rather than showing everything greyed out.
function applyProtocol() {
  const android = $('protocol').value === 'android';
  for (const id of ANDROID_ONLY) {
    const f = $(id).closest('.field');
    if (f) f.hidden = !android;
  }
  $('port-field').hidden = android;
  $('creds-row').hidden = android;
  $('pw-hint').hidden = android;
  $('port').required = !android;
  $('host-label').textContent = android ? 'Agent profile' : 'Host';
  applyAgentKind();
  if (!android) {
    $('host-hint').innerHTML = 'Resolved from inside this workspace, not from your laptop '
      + '&mdash; a machine on your own LAN needs a reachable address or a tunnel.';
  }
}
$('protocol').addEventListener('change', applyProtocol);

// The two channels want DIFFERENT things in `host`, and getting that wrong is
// the likeliest way to end up with a host that just times out: one wants a
// linked-host id, the other a remote-agent profile name.
function applyAgentKind() {
  const android = $('protocol').value === 'android';
  if (!android) return;
  const legacy = $('agent_kind').value === 'remote_agent';
  $('agent_base_url').closest('.field').hidden = !legacy;
  $('kind-hint').innerHTML = legacy
    ? 'Monolith path &mdash; reachable only from inside that deployment.'
    : 'Goes through aw-backend over the /link tunnel the host already holds open.';
  $('host-hint').innerHTML = legacy
    ? 'The remote-agent <b>profile name</b> (e.g. <code>macbook-fred</code>).'
    : 'The <b>linked host id</b> (see Remote Hosts) &mdash; not a hostname.';
}
$('agent_kind').addEventListener('change', applyAgentKind);

function resetForm() {
  $('id').value = '';
  $('form').reset();
  $('protocol').value = 'vnc';
  $('agent_kind').value = 'aw_remote_host';
  $('form-title').textContent = 'Add a host';
  $('save').textContent = 'Add host';
  $('cancel').hidden = true;
  $('pw-hint').textContent = '';
  applyProtocol();
}

function loadForEdit(id) {
  const h = hosts.find((x) => x.id === id);
  if (!h) return;
  $('id').value = h.id;
  $('name').value = h.name;
  $('protocol').value = h.protocol;
  $('host').value = h.host;
  $('port').value = h.port || '';
  $('username').value = h.username || '';
  $('password').value = '';
  for (const k of ANDROID_ONLY) $(k).value = h[k] || '';
  if (!h.agent_kind) $('agent_kind').value = 'aw_remote_host';
  $('form-title').textContent = 'Edit ' + h.name;
  $('save').textContent = 'Save changes';
  $('cancel').hidden = false;
  // The single most confusing thing about editing a saved credential is not
  // knowing whether leaving the field blank wipes it. Say so explicitly.
  $('pw-hint').textContent = h.has_password
    ? 'A password is saved. Leave blank to keep it; type a new one to replace it.'
    : 'No password saved for this host.';
  applyProtocol();
  $('name').focus();
  $('form-title').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('list').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  const del = b.getAttribute('data-del');
  const edit = b.getAttribute('data-edit');
  const confirmDel = b.getAttribute('data-confirm-del');
  if (edit) return loadForEdit(edit);
  if (b.hasAttribute('data-cancel-del')) { armed = null; return render(); }
  if (del) {
    const h = hosts.find((x) => x.id === del);
    armed = del;
    render();
    say('Deleting "' + esc(h ? h.name : del) + '" also deletes its saved password. Click Confirm.', 'err');
    return;
  }
  if (!confirmDel) return;
  armed = null;
  try {
    await call('DELETE', '/hosts/' + encodeURIComponent(confirmDel));
    if ($('id').value === confirmDel) resetForm();
    await refresh();
    say('Deleted.', 'ok');
  } catch (err) { say(esc(err.message), 'err'); }
});

$('cancel').addEventListener('click', () => { resetForm(); say('', 'ok'); });

$('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = $('id').value;
  const body = {
    name: $('name').value.trim(),
    protocol: $('protocol').value,
    host: $('host').value.trim(),
    port: Number($('port').value) || 0,
    username: $('username').value.trim(),
  };
  if ($('password').value) body.password = $('password').value;
  if (body.protocol === 'android') for (const k of ANDROID_ONLY) body[k] = $(k).value.trim();
  try {
    if (id) await call('PUT', '/hosts/' + encodeURIComponent(id), body);
    else await call('POST', '/hosts', body);
    resetForm();
    await refresh();
    say(id ? 'Saved.' : 'Host added.', 'ok');
  } catch (err) { say(esc(err.message), 'err'); }
});

applyProtocol();
refresh().catch((err) => say('Could not load hosts: ' + esc(err.message), 'err'));
