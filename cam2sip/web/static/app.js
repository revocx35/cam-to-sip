'use strict';

/* ---------- helpers ---------- */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const main = () => $('#main');

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch('/api' + path, opts);
  if (r.status === 401 && !['/login', '/session', '/setup'].includes(path)) {
    showAuth(false);
    throw new Error('Session expired - please sign in again');
  }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.blob();
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : `HTTP ${r.status}`);
  }
  return data;
}

function toast(msg, kind = '') {
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}

async function busy(btn, fn) {
  if (btn) { btn.classList.add('busy'); btn.disabled = true; }
  try { return await fn(); }
  catch (e) { toast(e.message, 'bad'); }
  finally { if (btn) { btn.classList.remove('busy'); btn.disabled = false; } }
}

function openModal(title, html, onMount) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal').classList.remove('hidden');
  if (onMount) onMount($('#modal-body'));
  const first = $('#modal-body input:not([type=hidden]), #modal-body select');
  if (first) first.focus();
}
function closeModal() { $('#modal').classList.add('hidden'); $('#modal-body').innerHTML = ''; }
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal' || e.target.closest('[data-close]')) closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

function readForm(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name || el.disabled) continue;
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
    else if (el.dataset.type === 'list') out[el.name] = el.value.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    else out[el.name] = el.value.trim();
  }
  return out;
}

const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString() : '-';
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
function ago(ts) {
  if (!ts) return 'never';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return `${Math.round(d)}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  return `${Math.round(d / 3600)}h ago`;
}
const badge = (text, kind = '', plain = false) => `<span class="badge ${kind} ${plain ? 'plain' : ''}">${esc(text)}</span>`;

function phoneBadge(st) {
  const s = (st && st.state) || 'disabled';
  const map = { registered: 'ok', registering: 'warn', failed: 'bad', stopped: '', disabled: '' };
  return badge(s, map[s] ?? '');
}

/* ---------- state ---------- */
const state = { cameras: [], phones: [], bridges: [], status: null, probes: {} };
async function loadAll() {
  const [cameras, phones, bridges, status] = await Promise.all([
    api('GET', '/cameras'), api('GET', '/phones'), api('GET', '/bridges'), api('GET', '/status')]);
  Object.assign(state, { cameras, phones, bridges, status });
}
const camById = id => state.cameras.find(c => c.id === id);
const phoneById = id => state.phones.find(p => p.id === id);

/* ---------- router ---------- */
let pollTimer = null;
let pageCleanup = null;
function every(ms, fn) { clearInterval(pollTimer); pollTimer = setInterval(() => { if (!document.hidden) fn(); }, ms); }
const pages = {};

async function route() {
  clearInterval(pollTimer);
  if (pageCleanup) { const c = pageCleanup; pageCleanup = null; c(); }
  const [name, arg] = location.hash.replace(/^#\/?/, '').split('/');
  const page = pages[name || 'dashboard'] ? (name || 'dashboard') : 'dashboard';
  const navPage = page === 'call' ? 'cameras' : page;
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === navPage));
  try { await pages[page](arg ? decodeURIComponent(arg) : undefined); }
  catch (e) { main().innerHTML = `<div class="empty"><p>${esc(e.message)}</p></div>`; }
}
window.addEventListener('beforeunload', () => { if (pageCleanup) pageCleanup(); });
window.addEventListener('hashchange', route);

/* ---------- dashboard ---------- */
function callCard(c) {
  const dur = c.answered_at ? fmtDur(Date.now() / 1000 - c.answered_at) : '';
  const m = c.media || {};
  const mic = m.mic || {}, spk = m.speaker || {};
  const stateBadge = c.state === 'active' ? badge(`in call ${dur}`, 'ok') : badge(c.state, 'warn');
  return `<div class="card">
    <div class="card-row"><h2>${esc(c.remote_display || c.remote || 'unknown')}</h2>${stateBadge}</div>
    <div class="muted">${c.direction === 'web' ? 'Browser call' : `${c.direction === 'inbound' ? 'Calling' : 'Called by'} ${esc(c.phone)} &middot; bridge ${esc(c.bridge || '-')}`}</div>
    <dl class="kv">
      <dt>Camera</dt><dd>${esc(c.camera || '-')}</dd>
      <dt>Codec</dt><dd>${esc(c.codec || '-')}</dd>
      <dt>Camera mic</dt><dd>${mic.connected ? badge(`live ${mic.codec || ''} · buffer ${mic.buffer_ms}ms`, 'ok') : badge(mic.error || 'connecting', mic.error ? 'bad' : 'warn')}</dd>
      <dt>Camera speaker</dt><dd>${spk.state === 'connected' ? badge(spk.gate_open ? 'talking' : 'connected', spk.gate_open ? 'info' : 'ok') : badge(spk.state || 'n/a', spk.state === 'error' ? 'bad' : '')}</dd>
      <dt>${c.direction === 'web' ? 'Frames' : 'RTP'}</dt><dd class="mono">${c.rtp.rx_packets} in / ${c.rtp.tx_packets} out ${c.rtp.remote ? '· ' + esc(c.rtp.remote) : ''}</dd>
    </dl>
    <div class="card-actions"><button class="btn danger" data-hangup="${esc(c.id)}">Hang up</button></div>
  </div>`;
}

function bindHangups(root) {
  $$('[data-hangup]', root).forEach(b => b.onclick = () => busy(b, async () => {
    await api('POST', `/calls/${b.dataset.hangup}/hangup`);
    toast('Call ended');
  }));
}

pages.dashboard = async () => {
  await loadAll();
  const render = () => {
    const st = state.status;
    const phonesReg = Object.values(st.phones).filter(p => p.state === 'registered').length;
    const setup = !state.cameras.length || !state.phones.length || !state.bridges.length;
    main().innerHTML = `
      <div class="page-head"><div><h1>Dashboard</h1><p class="muted">Camera ↔ SIP intercom bridge</p></div></div>
      <div class="tiles">
        <div class="card tile"><div class="label">go2rtc</div><div class="value">${st.go2rtc.online ? badge('online', 'ok') : badge('offline', 'bad')}</div><div class="muted small">${esc(st.go2rtc.version || '')}</div></div>
        <div class="card tile"><div class="label">Phones registered</div><div class="value">${phonesReg} / ${state.phones.filter(p => p.enabled).length}</div></div>
        <div class="card tile"><div class="label">Bridges</div><div class="value">${state.bridges.filter(b => b.enabled).length}</div></div>
        <div class="card tile"><div class="label">Active calls</div><div class="value">${st.calls.length}</div></div>
      </div>
      ${setup ? `<div class="card section"><h2>Getting started</h2>
        <ol>
          <li>${state.cameras.length ? '✅' : ''} <a href="#/cameras">Add a camera</a> (Tapo, ONVIF or any go2rtc source).</li>
          <li>${state.phones.length ? '✅' : ''} <a href="#/phones">Create a virtual phone</a> that registers to your PBX as an extension.</li>
          <li>${state.bridges.length ? '✅' : ''} <a href="#/bridges">Bridge</a> the camera to the phone.</li>
          <li>Call the extension from any phone: you hear the camera microphone and talk through its speaker.</li>
        </ol></div>` : ''}
      <div class="section"><h2>Active calls</h2>
        ${st.calls.length ? `<div class="grid">${st.calls.map(callCard).join('')}</div>` : '<div class="empty"><p>No active calls.</p></div>'}
      </div>
      ${state.cameras.length ? `<div class="section"><h2>Cameras</h2><div class="table-wrap"><table>
        <tr><th>Camera</th><th>Microphone</th><th>Speaker</th><th>Status</th><th></th></tr>
        ${state.cameras.map(c => {
          const p = c.probe || {};
          const busy = st.busy_cameras.includes(c.id);
          return `<tr><td>${esc(c.name)}</td><td>${p.mic ? badge(p.mic, 'ok') : badge('?', '')}</td>
            <td>${p.speaker ? badge(p.speaker, 'ok') : badge(c.probe ? 'none' : '?', c.probe ? 'warn' : '')}</td>
            <td>${!c.enabled ? badge('disabled') : busy ? badge('in call', 'info') : badge('ready', 'ok')}</td>
            <td><a class="btn small primary" href="#/call/${esc(c.id)}" ${busy || !c.enabled ? 'aria-disabled="true" style="pointer-events:none;opacity:.5"' : ''}>Call from browser</a></td></tr>`;
        }).join('')}
      </table></div></div>` : ''}
      ${state.bridges.length ? `<div class="section"><h2>Bridges</h2><div class="table-wrap"><table>
        <tr><th>Bridge</th><th>Camera</th><th>Phone</th><th>Registration</th><th>Status</th><th></th></tr>
        ${state.bridges.map(b => {
          const cam = camById(b.camera_id), ph = phoneById(b.phone_id);
          const inCall = st.busy_cameras.includes(b.camera_id);
          return `<tr><td>${esc(b.name || b.id)}</td><td>${esc(cam?.name || '?')}</td>
            <td>${esc(ph ? `${ph.username}@${ph.server}` : '?')}</td>
            <td>${phoneBadge(st.phones[b.phone_id])}</td>
            <td>${!b.enabled ? badge('disabled') : inCall ? badge('in call', 'info') : badge('ready', 'ok')}</td>
            <td><button class="btn small" data-dial="${esc(b.id)}">Call…</button></td></tr>`;
        }).join('')}
      </table></div></div>` : ''}`;
    bindHangups(main());
    $$('[data-dial]').forEach(btn => btn.onclick = () => dialDialog(btn.dataset.dial));
  };
  render();
  every(2000, async () => {
    if ($('#modal:not(.hidden)')) return;
    state.status = await api('GET', '/status');
    render();
  });
};

/* ---------- cameras ---------- */
function camCaps(c) {
  const p = state.probes[c.id] || c.probe;
  const out = [badge(c.kind.toUpperCase(), 'info', true)];
  if (!c.enabled) out.push(badge('disabled'));
  if (c.busy) out.push(badge('in call', 'info'));
  if (p) {
    out.push(p.mic ? badge(`mic ${p.mic}`, 'ok') : badge('no mic', 'bad'));
    out.push(p.speaker ? badge(`speaker ${p.speaker}`, 'ok') : badge('no speaker', 'warn'));
  } else if (c.kind === 'tapo' && !c.cloud_password_set) {
    out.push(badge('speaker needs cloud password', 'warn'));
  }
  return out.join('');
}

pages.cameras = async () => {
  state.cameras = await api('GET', '/cameras');
  const cams = state.cameras;
  main().innerHTML = `
    <div class="page-head"><div><h1>Cameras</h1><p class="muted">Audio sources and speakers, via go2rtc.</p></div>
      <button class="btn primary" id="add-cam">+ Add camera</button></div>
    ${cams.length ? `<div class="grid">${cams.map(c => `
      <div class="card" data-cam="${esc(c.id)}">
        <div class="snapshot-wrap"><img class="snapshot" alt="" data-snap="${esc(c.id)}"><div class="ph">loading snapshot…</div></div>
        <div class="card-row"><h2>${esc(c.name)}</h2></div>
        <div class="muted mono">${esc(c.kind === 'custom' ? 'custom go2rtc source' : `${c.host}`)}</div>
        <div class="badges">${camCaps(c)}</div>
        <div class="audio-slot"></div>
        <div class="card-actions">
          <button class="btn small primary" data-act="call" ${c.busy || !c.enabled ? 'disabled' : ''}>Call</button>
          <button class="btn small" data-act="probe">Check</button>
          <button class="btn small" data-act="listen">Listen 4s</button>
          <button class="btn small" data-act="speaker">Test speaker</button>
          <button class="btn small" data-act="edit">Edit</button>
          <button class="btn small danger" data-act="delete">Delete</button>
        </div>
      </div>`).join('')}</div>`
    : `<div class="empty"><p>No cameras yet.</p><button class="btn primary" id="add-cam-2">Add your first camera</button></div>`}`;
  $('#add-cam').onclick = () => cameraForm();
  if ($('#add-cam-2')) $('#add-cam-2').onclick = () => cameraForm();
  $$('[data-snap]').forEach(img => {
    img.onload = () => img.nextElementSibling.remove();
    img.onerror = () => { img.nextElementSibling.textContent = 'no snapshot'; };
    img.src = `/api/cameras/${img.dataset.snap}/snapshot.jpg?t=${Date.now()}`;
  });
  $$('[data-cam]').forEach(card => {
    const cam = camById(card.dataset.cam);
    $$('[data-act]', card).forEach(btn => btn.onclick = () => cameraAction(btn, cam, card));
  });
};

async function cameraAction(btn, cam, card) {
  const act = btn.dataset.act;
  if (act === 'call') { location.hash = `#/call/${encodeURIComponent(cam.id)}`; return; }
  if (act === 'edit') return cameraForm(cam);
  if (act === 'delete') {
    if (!confirm(`Delete camera "${cam.name}"?`)) return;
    return busy(btn, async () => { await api('DELETE', `/cameras/${cam.id}`); toast('Camera deleted'); pages.cameras(); });
  }
  if (act === 'probe') return busy(btn, async () => {
    const r = await api('POST', '/cameras/probe', { id: cam.id });
    state.probes[cam.id] = r;
    $('.badges', card).innerHTML = camCaps(cam);
    toast(r.errors.length ? r.errors.join('; ') : 'Microphone and speaker look good', r.errors.length ? 'bad' : 'ok');
  });
  if (act === 'speaker') return busy(btn, async () => {
    const r = await api('POST', `/cameras/${cam.id}/test-speaker`);
    toast(`Chime sent to the camera speaker (${r.codec}, ${r.packets} packets)`, 'ok');
  });
  if (act === 'listen') return busy(btn, async () => {
    const blob = await api('GET', `/cameras/${cam.id}/mic.wav?seconds=4`);
    const url = URL.createObjectURL(blob);
    $('.audio-slot', card).innerHTML = `<audio controls autoplay src="${url}" style="width:100%;margin-top:10px"></audio>`;
  });
}

function cameraForm(cam) {
  const c = cam || { kind: 'tapo', enabled: true, rtsp_port: 554, onvif_port: 2020, stream_path: 'stream2', mic_with_video: true };
  const secretPh = set => set ? 'unchanged (leave empty to keep)' : '';
  openModal(cam ? `Edit ${cam.name}` : 'Add camera', `
    <form id="cam-form" autocomplete="off">
      <div class="row">
        <label>Name<input name="name" required value="${esc(c.name)}" placeholder="Front door"></label>
        <label>Type<select name="kind">
          <option value="tapo" ${c.kind === 'tapo' ? 'selected' : ''}>TP-Link Tapo</option>
          <option value="onvif" ${c.kind === 'onvif' ? 'selected' : ''}>ONVIF / RTSP (backchannel)</option>
          <option value="custom" ${c.kind === 'custom' ? 'selected' : ''}>Custom go2rtc source</option>
        </select></label>
      </div>
      <div data-kinds="tapo onvif">
        <div class="row">
          <label>Host / IP<input name="host" value="${esc(c.host)}" placeholder="192.168.1.50"></label>
          <label>RTSP port<input name="rtsp_port" type="number" value="${esc(c.rtsp_port)}"></label>
        </div>
        <div class="row">
          <label>Camera username<input name="username" value="${esc(c.username)}" autocomplete="off"><span class="hint" data-kinds="tapo">The camera account from Tapo app → Advanced settings → Camera account.</span></label>
          <label>Camera password<input name="password" type="password" placeholder="${secretPh(c.password_set)}" autocomplete="new-password"></label>
        </div>
        <div class="row">
          <label>Stream path<input name="stream_path" value="${esc(c.stream_path)}" placeholder="stream2"><span class="hint">Path after host:port. Tapo: stream2 (SD) or stream1 (HD).</span></label>
          <label>ONVIF port<input name="onvif_port" type="number" value="${esc(c.onvif_port)}"><span class="hint">Tapo uses 2020. Used for discovery only.</span></label>
        </div>
        <div class="form-actions" style="justify-content:flex-start;margin:-4px 0 12px"><button type="button" class="btn small" id="discover">Discover streams (ONVIF)</button></div>
        <div id="discover-result"></div>
      </div>
      <div data-kinds="tapo">
        <label>TP-Link cloud password <span class="hint">Needed for talking through the speaker. It's your Tapo app account password; the camera account above doesn't work for this. If it's rejected, turn on Tapo app → Me → Tapo Lab → Third-Party Compatibility.</span>
          <input name="cloud_password" type="password" placeholder="${secretPh(c.cloud_password_set)}" autocomplete="new-password"></label>
      </div>
      <div data-kinds="custom">
        <div class="callout">Use any <a href="https://github.com/AlexxIT/go2rtc#module-streams" target="_blank" rel="noopener">go2rtc source</a>. The microphone source must provide audio. The speaker source must support two-way audio (RTSP ONVIF backchannel, tapo://, dvrip://, exec with backchannel…).</div>
        <label>Microphone source<input name="listen_url" value="${esc(c.listen_url)}" placeholder="rtsp://user:pass@192.168.1.50:554/stream"></label>
        <label>Speaker (backchannel) source<input name="talk_url" value="${esc(c.talk_url)}" placeholder="rtsp://user:pass@192.168.1.50:554/stream"></label>
      </div>
      <label class="check"><input type="checkbox" name="mic_with_video" ${c.mic_with_video ? 'checked' : ''}> Request video together with audio <span class="hint">(needed for Tapo, which only streams audio while video is playing)</span></label>
      <label class="check"><input type="checkbox" name="enabled" ${c.enabled ? 'checked' : ''}> Enabled</label>
      <div id="probe-result"></div>
      <div class="error" id="cam-error"></div>
      <div class="form-actions">
        <button type="button" class="btn" id="cam-test">Test connection</button>
        <button type="submit" class="btn primary">${cam ? 'Save' : 'Add camera'}</button>
      </div>
    </form>`, body => {
    const form = $('#cam-form', body);
    const sync = () => {
      const kind = form.kind.value;
      $$('[data-kinds]', form).forEach(el => el.classList.toggle('hidden', !el.dataset.kinds.split(' ').includes(kind)));
    };
    form.kind.onchange = () => {
      if (form.kind.value === 'onvif' && form.stream_path.value === 'stream2') form.stream_path.value = '';
      if (form.kind.value === 'tapo' && !form.stream_path.value) form.stream_path.value = 'stream2';
      sync();
    };
    sync();
    const payload = () => Object.assign(readForm(form), cam ? { id: cam.id } : {});
    $('#discover', form).onclick = e => busy(e.target, async () => {
      const f = readForm(form);
      const r = await api('POST', '/cameras/onvif-discover', { host: f.host, port: f.onvif_port || 80, username: f.username, password: f.password, id: cam?.id });
      const dev = r.device || {};
      $('#discover-result', form).innerHTML = `<div class="result"><b>${esc(dev.Manufacturer)} ${esc(dev.Model)}</b> <span class="muted">fw ${esc(dev.FirmwareVersion)}</span>
        ${(r.profiles || []).map((p, i) => `<div class="card-row" style="margin-top:6px"><span>${esc(p.name || p.token)} <span class="muted">${esc(p.video?.encoding || '')} ${esc(p.video?.width || '')}×${esc(p.video?.height || '')}, audio ${esc(p.audio?.encoding || 'none')}</span><br><span class="mono">${esc(p.rtsp)}</span></span>
        <button type="button" class="btn small" data-use="${i}">Use</button></div>`).join('')}</div>`;
      $$('[data-use]', form).forEach(b => b.onclick = () => {
        const p = r.profiles[Number(b.dataset.use)];
        try {
          const u = new URL(p.rtsp.replace(/^rtsp:/, 'http:'));
          form.stream_path.value = (u.pathname + u.search).replace(/^\//, '');
          form.rtsp_port.value = u.port || 554;
          toast(`Using ${p.name || p.token}`);
        } catch { toast('Could not parse RTSP URI', 'bad'); }
      });
    });
    $('#cam-test', form).onclick = e => busy(e.target, async () => {
      const r = await api('POST', '/cameras/probe', payload());
      $('#probe-result', form).innerHTML = `<div class="result">
        Microphone: ${r.mic ? badge(r.mic, 'ok') : badge('not found', 'bad')} &nbsp;
        Speaker: ${r.speaker ? badge(r.speaker, 'ok') : badge('not available', 'warn')}
        ${r.errors.length ? `<div class="error" style="margin-top:6px">${r.errors.map(esc).join('<br>')}</div>` : ''}</div>`;
    });
    form.onsubmit = e => {
      e.preventDefault();
      busy($('[type=submit]', form), async () => {
        try {
          if (cam) await api('PUT', `/cameras/${cam.id}`, payload());
          else await api('POST', '/cameras', payload());
        } catch (err) { $('#cam-error', form).textContent = err.message; return; }
        closeModal();
        toast(cam ? 'Camera saved' : 'Camera added', 'ok');
        pages.cameras();
      });
    };
  });
}

/* ---------- phones ---------- */
pages.phones = async () => {
  const render = () => {
    const phones = state.phones;
    main().innerHTML = `
      <div class="page-head"><div><h1>Virtual phones</h1><p class="muted">SIP extensions this server registers to your PBX.</p></div>
        <button class="btn primary" id="add-phone">+ Add phone</button></div>
      ${phones.length ? `<div class="grid">${phones.map(p => {
        const st = p.status || {};
        return `<div class="card" data-phone="${esc(p.id)}">
          <div class="card-row"><h2>${esc(p.name)}</h2>${p.enabled ? phoneBadge(st) : badge('disabled')}</div>
          <div class="mono muted">${esc(p.username)}@${esc(p.server)}:${esc(p.port)}</div>
          ${st.error ? `<div class="error small" style="margin-top:6px">${esc(st.error)}</div>` : ''}
          <dl class="kv">
            <dt>Display name</dt><dd>${esc(p.display_name || '-')}</dd>
            <dt>Last register</dt><dd>${esc(ago(st.last_register))}</dd>
            <dt>Contact</dt><dd class="mono">${esc(st.contact || '-')}</dd>
          </dl>
          <div class="card-actions">
            <button class="btn small" data-act="register">Re-register</button>
            <button class="btn small" data-act="edit">Edit</button>
            <button class="btn small danger" data-act="delete">Delete</button>
          </div></div>`;
      }).join('')}</div>`
      : `<div class="empty"><p>No virtual phones yet.</p><button class="btn primary" id="add-phone-2">Add a phone</button></div>`}`;
    $('#add-phone').onclick = () => phoneForm();
    if ($('#add-phone-2')) $('#add-phone-2').onclick = () => phoneForm();
    $$('[data-phone]').forEach(card => {
      const p = phoneById(card.dataset.phone);
      $$('[data-act]', card).forEach(btn => btn.onclick = () => {
        if (btn.dataset.act === 'edit') return phoneForm(p);
        if (btn.dataset.act === 'register') return busy(btn, async () => { await api('POST', `/phones/${p.id}/register`); toast('Registration refreshed'); });
        if (btn.dataset.act === 'delete' && confirm(`Delete phone "${p.name}"?`))
          busy(btn, async () => { await api('DELETE', `/phones/${p.id}`); toast('Phone deleted'); pages.phones(); });
      });
    });
  };
  state.phones = await api('GET', '/phones');
  render();
  every(3000, async () => { if ($('#modal:not(.hidden)')) return; state.phones = await api('GET', '/phones'); render(); });
};

function phoneForm(p) {
  const x = p || { port: 5060, expires: 300, enabled: true };
  openModal(p ? `Edit ${p.name}` : 'Add virtual phone', `
    <form id="phone-form" autocomplete="off">
      <div class="callout">Create the extension on your PBX first (FreePBX: Applications → Extensions → Add SIP [chan_pjsip] extension), then enter its number and secret here.</div>
      <label>Name<input name="name" required value="${esc(x.name)}" placeholder="Front door intercom"></label>
      <div class="row">
        <label>PBX host / IP<input name="server" required value="${esc(x.server)}" placeholder="192.168.1.10"></label>
        <label>SIP port (UDP)<input name="port" type="number" value="${esc(x.port)}"></label>
      </div>
      <div class="row">
        <label>Extension / username<input name="username" required value="${esc(x.username)}" placeholder="1008"></label>
        <label>Password / secret<input name="password" type="password" placeholder="${p?.password_set ? 'unchanged (leave empty to keep)' : ''}" autocomplete="new-password"></label>
      </div>
      <div class="row">
        <label>Display name<input name="display_name" value="${esc(x.display_name)}" placeholder="Front door"></label>
        <label>Auth username <span class="hint">optional, defaults to extension</span><input name="auth_username" value="${esc(x.auth_username)}"></label>
      </div>
      <div class="row">
        <label>SIP domain <span class="hint">optional, defaults to host</span><input name="domain" value="${esc(x.domain)}"></label>
        <label>Registration expiry (s)<input name="expires" type="number" min="60" max="3600" value="${esc(x.expires)}"></label>
      </div>
      <label class="check"><input type="checkbox" name="enabled" ${x.enabled ? 'checked' : ''}> Enabled (register to the PBX)</label>
      <div class="error" id="phone-error"></div>
      <div class="form-actions"><button type="submit" class="btn primary">${p ? 'Save' : 'Add phone'}</button></div>
    </form>`, body => {
    const form = $('#phone-form', body);
    form.onsubmit = e => {
      e.preventDefault();
      busy($('[type=submit]', form), async () => {
        try {
          if (p) await api('PUT', `/phones/${p.id}`, readForm(form));
          else await api('POST', '/phones', readForm(form));
        } catch (err) { $('#phone-error', form).textContent = err.message; return; }
        closeModal();
        toast(p ? 'Phone saved' : 'Phone added - registering…', 'ok');
        pages.phones();
      });
    };
  });
}

/* ---------- bridges ---------- */
pages.bridges = async () => {
  await loadAll();
  const render = () => {
    const st = state.status;
    main().innerHTML = `
      <div class="page-head"><div><h1>Bridges</h1><p class="muted">Connect a camera to a virtual phone.</p></div>
        <button class="btn primary" id="add-bridge" ${!state.cameras.length || !state.phones.length ? 'disabled title="Add a camera and a phone first"' : ''}>+ New bridge</button></div>
      ${state.bridges.length ? `<div class="grid">${state.bridges.map(b => {
        const cam = camById(b.camera_id), ph = phoneById(b.phone_id);
        const inCall = st.busy_cameras.includes(b.camera_id);
        return `<div class="card" data-bridge="${esc(b.id)}">
          <div class="card-row"><h2>${esc(b.name || `${cam?.name || '?'} ↔ ${ph?.username || '?'}`)}</h2>
            ${!b.enabled ? badge('disabled') : inCall ? badge('in call', 'info') : badge('ready', 'ok')}</div>
          <dl class="kv">
            <dt>Camera</dt><dd>${esc(cam?.name || 'missing')}</dd>
            <dt>Phone</dt><dd>${esc(ph ? `${ph.name} (${ph.username})` : 'missing')} ${phoneBadge(st.phones[b.phone_id])}</dd>
            <dt>Answer</dt><dd>${b.answer_delay ? `after ${b.answer_delay}s of ringing` : 'immediately'}</dd>
            <dt>Gain</dt><dd>mic ${b.mic_gain_db >= 0 ? '+' : ''}${b.mic_gain_db} dB · speaker ${b.speaker_gain_db >= 0 ? '+' : ''}${b.speaker_gain_db} dB</dd>
            <dt>Noise gate</dt><dd>${b.speaker_gate_db === null ? 'off' : `${b.speaker_gate_db} dBFS`}</dd>
            <dt>Callers</dt><dd>${b.allowed_callers.length ? esc(b.allowed_callers.join(', ')) : 'anyone'}</dd>
          </dl>
          <div class="card-actions">
            <button class="btn small" data-act="dial">Call…</button>
            <button class="btn small" data-act="edit">Edit</button>
            <button class="btn small danger" data-act="delete">Delete</button>
          </div></div>`;
      }).join('')}</div>`
      : `<div class="empty"><p>${state.cameras.length && state.phones.length ? 'No bridges yet.' : 'Add a camera and a virtual phone first.'}</p></div>`}`;
    $('#add-bridge').onclick = () => bridgeForm();
    $$('[data-bridge]').forEach(card => {
      const b = state.bridges.find(x => x.id === card.dataset.bridge);
      $$('[data-act]', card).forEach(btn => btn.onclick = () => {
        if (btn.dataset.act === 'edit') return bridgeForm(b);
        if (btn.dataset.act === 'dial') return dialDialog(b.id);
        if (btn.dataset.act === 'delete' && confirm('Delete this bridge?'))
          busy(btn, async () => { await api('DELETE', `/bridges/${b.id}`); toast('Bridge deleted'); pages.bridges(); });
      });
    });
  };
  render();
  every(3000, async () => { if ($('#modal:not(.hidden)')) return; state.status = await api('GET', '/status'); render(); });
};

function dtmfRow(a = {}) {
  return `<div class="dtmf-row">
    <input data-f="digit" maxlength="1" placeholder="1" value="${esc(a.digit)}">
    <select data-f="method"><option ${a.method === 'GET' ? 'selected' : ''}>GET</option><option ${a.method !== 'GET' ? 'selected' : ''}>POST</option></select>
    <input data-f="url" placeholder="http://homeassistant:8123/api/webhook/open-gate" value="${esc(a.url)}">
    <label class="check" style="margin:0" title="Hang up after the request"><input type="checkbox" data-f="hangup" ${a.hangup ? 'checked' : ''}>hang up</label>
    <button type="button" class="icon-btn" data-rm aria-label="Remove">&times;</button>
  </div>`;
}

function bridgeForm(b) {
  const x = b || { enabled: true, answer_delay: 0, mic_gain_db: 0, speaker_gain_db: 0, speaker_gate_db: -50, max_call_seconds: 600, allowed_callers: [], dtmf_actions: [], hangup_digit: '' };
  const usedPhones = new Set(state.bridges.filter(o => o.id !== b?.id).map(o => o.phone_id));
  openModal(b ? 'Edit bridge' : 'New bridge', `
    <form id="bridge-form" autocomplete="off">
      <label>Name <span class="hint">optional</span><input name="name" value="${esc(x.name)}" placeholder="Front door intercom"></label>
      <div class="row">
        <label>Camera<select name="camera_id" required>${state.cameras.map(c => `<option value="${esc(c.id)}" ${c.id === x.camera_id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select></label>
        <label>Virtual phone<select name="phone_id" required>${state.phones.map(p => `<option value="${esc(p.id)}" ${p.id === x.phone_id ? 'selected' : ''} ${usedPhones.has(p.id) ? 'disabled' : ''}>${esc(p.name)} (${esc(p.username)})${usedPhones.has(p.id) ? ' - already bridged' : ''}</option>`).join('')}</select></label>
      </div>
      <fieldset><legend>Answering</legend>
        <div class="row">
          <label>Ring before answering (s)<input name="answer_delay" type="number" min="0" max="60" step="0.5" value="${esc(x.answer_delay)}"></label>
          <label>Max call duration (s)<input name="max_call_seconds" type="number" min="10" value="${esc(x.max_call_seconds)}"></label>
        </div>
        <label>Allowed callers <span class="hint">one per line or comma separated; empty = anyone</span><textarea name="allowed_callers" data-type="list" rows="2" placeholder="1001, 1002">${esc(x.allowed_callers.join('\n'))}</textarea></label>
      </fieldset>
      <fieldset><legend>Audio</legend>
        <div class="row">
          <label>Camera mic → phone gain (dB)<input name="mic_gain_db" type="number" min="-20" max="30" step="1" value="${esc(x.mic_gain_db)}"></label>
          <label>Phone → camera speaker gain (dB)<input name="speaker_gain_db" type="number" min="-20" max="30" step="1" value="${esc(x.speaker_gain_db)}"></label>
        </div>
        <label class="check"><input type="checkbox" id="gate-on" ${x.speaker_gate_db !== null ? 'checked' : ''}> Noise gate on phone → speaker audio</label>
        <label>Gate threshold (dBFS) <span class="hint">Quieter phone audio isn't sent to the camera. Many cameras (Tapo included) mute their mic while the speaker plays, so gating line noise keeps you able to hear.</span>
          <input name="speaker_gate_db" type="number" min="-90" max="0" step="1" value="${esc(x.speaker_gate_db ?? -50)}"></label>
      </fieldset>
      <fieldset><legend>DTMF</legend>
        <label>Hang-up digit <span class="hint">optional, e.g. #</span><input name="hangup_digit" maxlength="1" value="${esc(x.hangup_digit)}" style="max-width:80px"></label>
        <div class="hint muted small" style="margin-bottom:6px">Keypad actions: send an HTTP request when a digit is pressed, e.g. open a gate through Home Assistant.</div>
        <div id="dtmf-list">${x.dtmf_actions.map(dtmfRow).join('')}</div>
        <button type="button" class="btn small" id="dtmf-add" style="margin-bottom:12px">+ Add action</button>
      </fieldset>
      <label class="check"><input type="checkbox" name="enabled" ${x.enabled ? 'checked' : ''}> Enabled</label>
      <div class="error" id="bridge-error"></div>
      <div class="form-actions"><button type="submit" class="btn primary">${b ? 'Save' : 'Create bridge'}</button></div>
    </form>`, body => {
    const form = $('#bridge-form', body);
    const gate = $('#gate-on', form);
    const syncGate = () => { form.speaker_gate_db.disabled = !gate.checked; };
    gate.onchange = syncGate; syncGate();
    const bindRm = () => $$('[data-rm]', form).forEach(btn => btn.onclick = () => btn.parentElement.remove());
    $('#dtmf-add', form).onclick = () => { $('#dtmf-list', form).insertAdjacentHTML('beforeend', dtmfRow()); bindRm(); };
    bindRm();
    form.onsubmit = e => {
      e.preventDefault();
      const data = readForm(form);
      data.speaker_gate_db = gate.checked ? data.speaker_gate_db : null;
      data.dtmf_actions = $$('.dtmf-row', form).map(r => ({
        digit: $('[data-f=digit]', r).value.trim(), method: $('[data-f=method]', r).value,
        url: $('[data-f=url]', r).value.trim(), hangup: $('[data-f=hangup]', r).checked,
      })).filter(a => a.digit && a.url);
      busy($('[type=submit]', form), async () => {
        try {
          if (b) await api('PUT', `/bridges/${b.id}`, data);
          else await api('POST', '/bridges', data);
        } catch (err) { $('#bridge-error', form).textContent = err.message; return; }
        closeModal();
        toast(b ? 'Bridge saved' : 'Bridge created', 'ok');
        pages.bridges();
      });
    };
  });
}

function dialDialog(bridgeId) {
  openModal('Call from camera', `
    <form id="dial-form">
      <p class="muted" style="margin-top:0">The virtual phone calls this number. When it's answered, the camera audio is bridged (intercom / doorbell style).</p>
      <label>Number or SIP URI<input name="target" required placeholder="1001"></label>
      <div class="form-actions"><button class="btn primary" type="submit">Call</button></div>
    </form>`, body => {
    const form = $('#dial-form', body);
    form.onsubmit = e => {
      e.preventDefault();
      busy($('[type=submit]', form), async () => {
        await api('POST', `/bridges/${bridgeId}/call`, { target: form.target.value.trim() });
        closeModal();
        toast('Calling…', 'ok');
      });
    };
  });
}

/* ---------- calls ---------- */
pages.calls = async () => {
  const render = data => {
    main().innerHTML = `
      <div class="page-head"><div><h1>Calls</h1></div></div>
      <div class="section" style="margin-top:0"><h2>Active</h2>
        ${data.active.length ? `<div class="grid">${data.active.map(callCard).join('')}</div>` : '<div class="empty"><p>No active calls.</p></div>'}</div>
      <div class="section"><h2>History</h2>
        ${data.history.length ? `<div class="table-wrap"><table>
          <tr><th>Time</th><th>Direction</th><th>Remote</th><th>Phone</th><th>Camera</th><th>Duration</th><th>Codec</th><th>Result</th></tr>
          ${data.history.map(h => `<tr><td>${esc(fmtTime(h.started_at))}</td><td>${h.direction === 'web' ? 'web' : h.direction === 'inbound' ? '↓ in' : '↑ out'}</td>
            <td>${esc(h.remote_display ? `${h.remote_display} (${h.remote})` : h.remote)}</td><td>${esc(h.phone)}</td><td>${esc(h.camera || '-')}</td>
            <td>${h.answered_at ? fmtDur(h.duration) : '-'}</td><td>${esc(h.codec || '-')}</td><td>${esc(h.result)}</td></tr>`).join('')}
        </table></div>` : '<div class="empty"><p>No calls yet.</p></div>'}</div>`;
    bindHangups(main());
  };
  render(await api('GET', '/calls'));
  every(2000, async () => render(await api('GET', '/calls')));
};

/* ---------- logs ---------- */
pages.logs = async () => {
  main().innerHTML = `
    <div class="page-head"><div><h1>Logs</h1></div>
      <div class="toolbar">
        <select id="log-level"><option value="0">All levels</option><option value="1">Warnings &amp; errors</option></select>
        <input id="log-filter" placeholder="Filter…">
        <label class="check" style="margin:0"><input type="checkbox" id="log-follow" checked> Follow</label>
        <button class="btn small" id="log-clear">Clear view</button>
      </div></div>
    <div class="logbox" id="logbox"></div>`;
  const box = $('#logbox');
  let seq = 0, records = [];
  const draw = () => {
    const lvl = $('#log-level').value, f = $('#log-filter').value.toLowerCase();
    const rows = records.filter(r => (lvl === '0' || ['WARNING', 'ERROR', 'CRITICAL'].includes(r.level))
      && (!f || r.msg.toLowerCase().includes(f) || r.logger.includes(f)));
    box.innerHTML = rows.map(r => `<div class="l ${r.level}"><span class="t">${esc(new Date(r.ts * 1000).toLocaleTimeString())} ${esc(r.level.padEnd(7))} ${esc(r.logger)}</span> ${esc(r.msg)}</div>`).join('');
    if ($('#log-follow').checked) box.scrollTop = box.scrollHeight;
  };
  const poll = async () => {
    const r = await api('GET', `/logs?after=${seq}`);
    if (r.records.length) {
      records = records.concat(r.records).slice(-2000);
      seq = r.records[r.records.length - 1].seq;
      draw();
    }
  };
  $('#log-level').onchange = draw;
  $('#log-filter').oninput = draw;
  $('#log-clear').onclick = () => { records = []; draw(); };
  await poll();
  every(1500, poll);
};

/* ---------- browser call ---------- */
pages.call = async id => {
  state.cameras = await api('GET', '/cameras');
  const cam = camById(id);
  if (!cam) { main().innerHTML = '<div class="empty"><p>Camera not found.</p></div>'; return; }
  const secureUrl = `https://${location.hostname}:${state.httpsPort}${location.pathname}${location.hash}`;
  main().innerHTML = `
    <div class="page-head"><div><h1>${esc(cam.name)}</h1><p class="muted" id="call-sub">Connecting…</p></div>
      <div class="toolbar"><span class="badge plain" id="call-timer">0:00</span>
        <button class="btn danger solid" id="call-hangup">Hang up</button></div></div>
    <div class="call-layout">
      <div class="call-video">
        <video id="call-video" autoplay muted playsinline></video>
        <img id="call-img" class="hidden" alt="">
        <div class="call-overlay" id="call-overlay">Connecting to camera…</div>
      </div>
      <div class="card call-side">
        <div class="callout warn hidden" id="mic-warning"></div>
        <button class="ptt" id="ptt" disabled><span class="ptt-label">Hold to talk</span><small>or hold the space bar</small></button>
        <label class="check"><input type="checkbox" id="open-mic" disabled> Open microphone (hands-free)</label>
        <div class="meter"><span>Camera</span><div class="bar"><i id="lvl-cam"></i></div></div>
        <div class="meter"><span>You</span><div class="bar"><i id="lvl-mic"></i></div></div>
        <label>Camera volume<input type="range" id="vol-cam" min="0" max="300" value="100"></label>
        <label>Your voice on the camera<input type="range" id="vol-mic" min="25" max="400" value="100"></label>
        <dl class="kv" id="call-stats"></dl>
        <p class="muted small">Many cameras mute their microphone while their speaker plays, so push-to-talk works best.
          Use headphones to avoid echo in hands-free mode.</p>
      </div>
    </div>`;
  const overlay = $('#call-overlay'), sub = $('#call-sub'), ptt = $('#ptt'), openMic = $('#open-mic');
  let started = Date.now();
  const call = new CameraCall(cam.id, {
    onState: (st, detail) => {
      if (st === 'connected') { sub.textContent = 'Connected'; started = Date.now(); }
      if (st === 'error' || st === 'ended') {
        sub.textContent = detail ? `Call ended: ${detail}` : 'Call ended';
        overlay.textContent = detail || 'Call ended';
        overlay.classList.remove('hidden');
        ptt.disabled = openMic.disabled = true;
        $('#call-hangup').textContent = 'Back';
      }
    },
    onStatus: m => {
      const mic = m.mic || {}, spk = m.speaker || {};
      $('#call-stats').innerHTML = `
        <dt>Camera mic</dt><dd>${mic.connected ? badge(`live ${mic.codec || ''}`, 'ok') : badge(mic.error || 'connecting', mic.error ? 'bad' : 'warn')}</dd>
        <dt>Camera speaker</dt><dd>${spk.state === 'connected' ? badge(spk.gate_open ? 'talking' : 'connected', spk.gate_open ? 'info' : 'ok') : badge(spk.state === 'none' ? 'not available' : spk.state, spk.state === 'error' ? 'bad' : '')}</dd>`;
      if (spk.state === 'none') { ptt.disabled = openMic.disabled = true; ptt.querySelector('.ptt-label').textContent = 'No speaker'; }
    },
    onVideo: kind => { if (kind === 'live') overlay.classList.add('hidden'); },
  });
  $('#call-video').addEventListener('playing', () => overlay.classList.add('hidden'));
  $('#call-img').addEventListener('load', () => overlay.classList.add('hidden'));
  const timer = setInterval(() => { $('#call-timer').textContent = fmtDur((Date.now() - started) / 1000); }, 1000);
  const meters = setInterval(() => {
    $('#lvl-cam').style.width = `${Math.min(100, call.levels.camera * 400)}%`;
    $('#lvl-mic').style.width = `${Math.min(100, call.levels.mic * 400)}%`;
  }, 100);

  const unlockAudio = () => { if (call.suspended) call.resume(); if (!overlay.querySelector('#enable-audio')) return; overlay.classList.add('hidden'); };
  const setTalk = on => {
    if (ptt.disabled || call.talking === on) return;
    unlockAudio();
    call.setTalking(on);
    ptt.classList.toggle('active', on);
    ptt.querySelector('.ptt-label').textContent = on ? 'Talking…' : 'Hold to talk';
  };
  ptt.addEventListener('pointerdown', e => { e.preventDefault(); ptt.setPointerCapture(e.pointerId); setTalk(true); });
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(ev => ptt.addEventListener(ev, () => setTalk(false)));
  const onKey = e => {
    if (e.code !== 'Space' || e.target.closest('input, textarea, select')) return;
    e.preventDefault();
    if (!e.repeat) setTalk(e.type === 'keydown');
  };
  document.addEventListener('keydown', onKey);
  document.addEventListener('keyup', onKey);
  openMic.onchange = () => { unlockAudio(); call.setOpenMic(openMic.checked); ptt.classList.toggle('open', openMic.checked); };
  $('#vol-cam').oninput = e => call.setVolume(e.target.value / 100);
  $('#vol-mic').oninput = e => { call.micGain = e.target.value / 100; };
  $('#call-hangup').onclick = () => { location.hash = '#/cameras'; };

  pageCleanup = () => {
    clearInterval(timer); clearInterval(meters);
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('keyup', onKey);
    call.stop();
  };

  const mic = await call.start($('#call-video'), $('#call-img'));
  const warn = $('#mic-warning');
  if (mic === 'ready') { ptt.disabled = openMic.disabled = false; }
  else if (mic === 'insecure') {
    warn.innerHTML = state.httpsPort
      ? `Browsers only allow the microphone on secure pages, so you can listen here but not talk.
         <a href="${esc(secureUrl)}">Open this call over HTTPS</a> and accept the self-signed certificate once.`
      : 'Browsers only allow the microphone on secure (HTTPS) pages. Serve the UI over HTTPS to talk; listening works here.';
    warn.classList.remove('hidden');
  } else {
    warn.textContent = `Microphone unavailable (${call.micError || mic}). Allow microphone access for this site to talk.`;
    warn.classList.remove('hidden');
  }
  if (call.suspended) {
    overlay.innerHTML = '<button class="btn primary" id="enable-audio">Tap to start audio</button>';
    overlay.classList.remove('hidden');
    $('#enable-audio').onclick = async () => { await call.resume(); overlay.classList.add('hidden'); };
  }
};

/* ---------- settings ---------- */
pages.settings = async () => {
  const s = await api('GET', '/settings');
  const origin = location.origin;
  main().innerHTML = `
    <div class="page-head"><div><h1>Settings</h1></div></div>
    <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(340px,1fr))">
      <div class="card"><h2>System</h2><dl class="kv">
        <dt>Version</dt><dd>${esc(s.version)}</dd>
        <dt>Web UI port</dt><dd>${esc(s.web_port)}</dd>
        <dt>SIP (UDP)</dt><dd>${esc(s.sip_port)}</dd>
        <dt>RTP ports</dt><dd>${esc(s.rtp_ports)}</dd>
        <dt>Advertised IP</dt><dd>${esc(s.advertise_ip)}</dd>
        <dt>go2rtc API</dt><dd class="mono">${esc(s.go2rtc_api)}</dd>
        <dt>Data dir</dt><dd class="mono">${esc(s.data_dir)}</dd>
      </dl><p class="muted small">These come from environment variables (see .env / docker-compose.yml).</p></div>
      <div class="card"><h2>Change admin password</h2>
        <form id="pw-form" style="margin-top:10px">
          <label>Current password<input name="current" type="password" required autocomplete="current-password"></label>
          <label>New password<input name="new" type="password" required minlength="6" autocomplete="new-password"></label>
          <div class="error" id="pw-error"></div>
          <div class="form-actions"><button class="btn primary" type="submit">Change password</button></div>
        </form></div>
    </div>
    <div class="card section"><h2>Automation API</h2>
      <p class="muted">Use this token to trigger calls from Home Assistant, Frigate, Node-RED, etc.
        Full API reference: <a href="/api/docs" target="_blank">/api/docs</a>.</p>
      <div class="toolbar"><input id="token" class="mono" readonly value="${esc(s.api_token)}" style="flex:1;min-width:240px">
        <button class="btn small" id="copy-token">Copy</button><button class="btn small danger" id="regen-token">Regenerate</button></div>
      <h3 style="margin-top:16px">Example: make a bridge call extension 1001</h3>
      <pre class="logbox" style="height:auto;min-height:0">curl -X POST ${esc(origin)}/api/bridges/&lt;bridge-id&gt;/call \\
  -H "Authorization: Bearer ${esc(s.api_token)}" \\
  -H "Content-Type: application/json" -d '{"target": "1001"}'</pre>
      <p class="muted small">Bridge ids: ${state.bridges.length ? state.bridges.map(b => `<code>${esc(b.id)}</code> (${esc(b.name || '')})`).join(', ') : 'create a bridge first'}.</p>
    </div>`;
  $('#copy-token').onclick = () => { navigator.clipboard?.writeText($('#token').value); toast('Copied'); };
  $('#regen-token').onclick = e => {
    if (!confirm('Regenerate the API token? Existing integrations will stop working.')) return;
    busy(e.target, async () => { const r = await api('POST', '/settings/api-token'); $('#token').value = r.api_token; toast('New token generated', 'ok'); });
  };
  const form = $('#pw-form');
  form.onsubmit = e => {
    e.preventDefault();
    busy($('[type=submit]', form), async () => {
      try { await api('POST', '/settings/password', readForm(form)); }
      catch (err) { $('#pw-error').textContent = err.message; return; }
      form.reset(); $('#pw-error').textContent = '';
      toast('Password changed', 'ok');
    });
  };
};

/* ---------- auth ---------- */
function showAuth(setup) {
  $('#app').classList.add('hidden');
  $('#auth').classList.remove('hidden');
  $('#auth-hint').textContent = setup ? 'Welcome! Choose an admin password for the web UI.' : 'Sign in to manage your camera bridges.';
  $('#auth-confirm-wrap').classList.toggle('hidden', !setup);
  $('#auth-submit').textContent = setup ? 'Create password' : 'Sign in';
  $('#auth-form').dataset.setup = setup ? '1' : '';
  $('#auth-password').focus();
}

$('#auth-form').onsubmit = async e => {
  e.preventDefault();
  const setup = !!e.target.dataset.setup;
  const pw = $('#auth-password').value;
  $('#auth-error').textContent = '';
  if (setup && pw !== $('#auth-confirm').value) { $('#auth-error').textContent = 'Passwords do not match'; return; }
  try {
    await api('POST', setup ? '/setup' : '/login', { password: pw });
    $('#auth-password').value = ''; $('#auth-confirm').value = '';
    start();
  } catch (err) { $('#auth-error').textContent = err.message; }
};

$('#logout').onclick = async () => { await api('POST', '/logout'); showAuth(false); };

async function start() {
  const s = await api('GET', '/session');
  state.httpsPort = s.https_port;
  $('#version').textContent = `v${s.version}`;
  if (!s.authenticated) return showAuth(s.setup_required);
  $('#auth').classList.add('hidden');
  $('#app').classList.remove('hidden');
  await loadAll().catch(() => {});
  route();
}
start();
