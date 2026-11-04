'use strict';
// Baird's Hoard UI: live picture (fragmented MP4 over a WebSocket into Media Source Extensions), the three capture
// buttons, the recent files (drag them out or copy them) and the settings.

const $ = (id) => document.getElementById(id);
const video = $('video');
const state = { kind: 'all', items: [], fresh: new Set(), status: null, settings: null, lastListKey: '' };

// ---------------------------------------------------------------- helpers

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.hint = data.hint || '';
    throw err;
  }
  return data;
}

function toast(text, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = text;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 6000 : 3200);
}

function fail(err) { toast(err.hint ? `${err.message} ${err.hint}` : err.message, 'err'); }

function fmtSize(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
  return `${n} B`;
}

function fmtDur(s) {
  if (s == null) return '';
  s = Math.round(s);
  const m = Math.floor(s / 60), r = s % 60;
  return `${m}:${String(r).padStart(2, '0')}`;
}

function fmtTime(iso) {
  const d = new Date(iso);
  const today = new Date();
  const hm = d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  return d.toDateString() === today.toDateString() ? hm : `${d.toLocaleDateString('es-ES')} ${hm}`;
}

// ---------------------------------------------------------------- live picture

class LivePlayer {
  constructor(el) {
    this.el = el; this.ws = null; this.ms = null; this.sb = null; this.queue = []; this.mime = '';
    this.retry = 1000; this.wantInit = false;
    setInterval(() => this.chase(), 400);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) this.chase(true); });
  }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/api/live`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => { this.retry = 1000; };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'init') this.setup(msg.mime);
        else if (msg.type === 'reset') this.teardown();
        return;
      }
      this.queue.push(new Uint8Array(ev.data));
      this.pump();
    };
    ws.onclose = () => {
      this.teardown();
      setTimeout(() => this.connect(), this.retry);
      this.retry = Math.min(this.retry * 1.6, 8000);
    };
  }

  setup(mime) {
    this.teardown();
    let type = mime;
    if (!window.MediaSource || !MediaSource.isTypeSupported(type)) {
      type = mime.replace(/,\s*mp4a\.[0-9.]+/, '');
    }
    this.mime = type;
    const ms = new MediaSource();
    this.ms = ms;
    this.el.src = URL.createObjectURL(ms);
    ms.addEventListener('sourceopen', () => {
      if (this.ms !== ms) return;
      try {
        this.sb = ms.addSourceBuffer(type);
        this.sb.mode = 'segments';
        this.sb.addEventListener('updateend', () => this.pump());
        this.pump();
      } catch (e) {
        console.error('MSE', e);
      }
    }, { once: true });
    this.el.play().catch(() => {});
  }

  teardown() {
    this.queue = [];
    this.sb = null;
    if (this.ms) {
      try { if (this.ms.readyState === 'open') this.ms.endOfStream(); } catch { /* ignore */ }
      URL.revokeObjectURL(this.el.src);
      this.ms = null;
    }
  }

  pump() {
    const sb = this.sb;
    if (!sb || sb.updating || !this.queue.length) return;
    // Trim what is far behind so memory stays flat during long sessions.
    const b = sb.buffered;
    if (b.length && this.el.currentTime - b.start(0) > 40 && !this.trimming) {
      this.trimming = true;
      try { sb.remove(b.start(0), this.el.currentTime - 20); return; } catch { /* ignore */ } finally { this.trimming = false; }
    }
    const chunk = this.queue.length > 1 ? concat(this.queue.splice(0)) : this.queue.shift();
    try { sb.appendBuffer(chunk); } catch (e) {
      if (e.name === 'QuotaExceededError') { this.queue.unshift(chunk); return; }
      console.warn('append', e); this.teardown(); if (this.ws) this.ws.close();
    }
  }

  // Stay at the live edge: jump when more than ~1 s behind.
  chase(force) {
    const sb = this.sb;
    if (!sb || !sb.buffered.length) return;
    const end = sb.buffered.end(sb.buffered.length - 1);
    const lag = end - this.el.currentTime;
    if (lag > 1.0 || force && lag > 0.3) this.el.currentTime = Math.max(sb.buffered.start(0), end - 0.15);
    if (this.el.paused) this.el.play().catch(() => {});
  }
}

function concat(parts) {
  const n = parts.reduce((a, p) => a + p.byteLength, 0);
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.byteLength; }
  return out;
}

const player = new LivePlayer(video);
player.connect();

// ---------------------------------------------------------------- status

const STATE_TEXT = {
  running: 'En directo', starting: 'Conectando…', no_signal: 'Sin señal', no_device: 'Sin capturadora',
  off: 'Captura apagada', error: 'Error', stuck: 'Bloqueada',
};
const STATE_HINT = {
  starting: ['Abriendo la capturadora…', ''],
  no_signal: ['Sin señal', 'El otro ordenador está apagado, en reposo o no manda imagen por HDMI.'],
  no_device: ['Capturadora no conectada', 'Conecta la AVerMedia Live Gamer Portable Lite por USB.'],
  off: ['Captura apagada', 'Actívala en Ajustes.'],
  error: ['No se pudo abrir la capturadora', ''],
  stuck: ['La capturadora no responde',
    'Desenchufa su USB, espera cinco segundos y vuelve a enchufarlo. Baird la recupera solo.'],
};

async function refreshStatus() {
  let st;
  try { st = await api('/api/status'); } catch { setChip('error', 'Hoard sin respuesta'); return; }
  state.status = st;
  const cap = st.capture || {};
  const s = cap.state || 'off';
  let text = STATE_TEXT[s] || s;
  if (s === 'running' && cap.input) text += ` · ${cap.input.width}×${cap.input.height}`;
  setChip(s, text);
  $('meta').textContent = s === 'running'
    ? `${(cap.kbps / 1000).toFixed(1)} Mb/s · ${Math.round(cap.buffered_seconds || 0)} s en memoria`
    : '';
  const msg = STATE_HINT[s];
  $('stage-msg').hidden = s === 'running' || !msg;
  if (msg) {
    $('stage-msg-title').textContent = msg[0];
    $('stage-msg-hint').textContent = s === 'error' ? (cap.message || '') : msg[1];
  }
  const live = s === 'running';
  $('btn-shot').disabled = !live;
  $('btn-clip').disabled = !live;
  $('clip-label').textContent = `Últimos ${st.clip_seconds} s`;

  const rec = st.recording || {};
  const recording = rec.state === 'recording';
  $('btn-rec').disabled = !live && !recording || rec.state === 'finishing';
  $('btn-rec').classList.toggle('recording', recording);
  $('rec-label').textContent = recording ? 'Parar' : rec.state === 'finishing' ? 'Guardando…' : 'Grabar';
  $('rec-badge').hidden = !recording;
  if (recording) $('rec-time').textContent = `${fmtDur(rec.seconds)} · ${fmtSize(rec.bytes || 0)}`;
  if (state.recWas === 'finishing' && rec.state === 'idle' && rec.last) {
    if (rec.last.error) toast(`La grabación no se pudo convertir: ${rec.last.error}`, 'err');
    else { toast(`Grabación guardada: ${rec.last.name}`, 'ok'); markFresh(rec.last.name); }
    refreshList(true);
  }
  state.recWas = rec.state;
  for (const job of st.jobs || []) {
    const seen = state.jobsDone || (state.jobsDone = new Set());
    if (job.state !== 'running' && !seen.has(job.id)) {
      seen.add(job.id);
      if (state.jobsStarted && state.jobsStarted.has(job.id)) {
        if (job.state === 'done') { toast(`Copia ligera lista: ${job.target}`, 'ok'); markFresh(job.target); refreshList(true); }
        else toast(`La copia ligera falló: ${job.message}`, 'err');
      }
    }
  }
}

function setChip(s, text) {
  $('chip').dataset.state = s;
  $('chip-text').textContent = text;
}

// ---------------------------------------------------------------- recent files

function markFresh(name) {
  if (!name) return;
  state.fresh.add(name);
  setTimeout(() => { state.fresh.delete(name); renderList(); }, 15000);
}

async function refreshList(force) {
  let data;
  try { data = await api(`/api/media?kind=${state.kind}&limit=120`); } catch { return; }
  const key = data.items.map((i) => `${i.name}|${i.size}|${i.modified}`).join(',');
  if (!force && key === state.lastListKey) return;
  state.lastListKey = key;
  state.items = data.items;
  renderList();
}

function renderList() {
  const ul = $('items');
  ul.textContent = '';
  $('empty').hidden = state.items.length > 0;
  for (const item of state.items) ul.appendChild(card(item));
}

function card(item) {
  const li = document.createElement('li');
  li.className = 'item' + (state.fresh.has(item.name) ? ' fresh' : '');
  li.draggable = true;
  li.title = `${item.name}\nArrástrala a un chat o a una carpeta`;
  const isVideo = item.kind === 'video';
  const sub = [fmtTime(item.modified), fmtSize(item.size)];
  if (item.width) sub.push(`${item.width}×${item.height}`);
  li.innerHTML = `
    <div class="thumb"><img loading="lazy" alt="">${isVideo ? `<span class="dur">${fmtDur(item.duration)}</span>` : ''}</div>
    <div class="info">
      <div class="name"></div>
      <div class="sub"></div>
      <div class="acts">
        <button class="btn small" data-act="copy" title="Copiar el archivo (pega con Ctrl+V)">Copiar</button>
        <button class="btn small" data-act="reveal" title="Mostrar en el Explorador">Explorador</button>
        ${isVideo && item.origin !== 'light' ? '<button class="btn small" data-act="light" title="Copia de 720p para chats con límite de tamaño">Ligera</button>' : ''}
        <button class="btn small danger" data-act="delete" title="Enviar a la papelera">Borrar</button>
      </div>
    </div>`;
  li.querySelector('img').src = item.thumb;
  li.querySelector('.name').textContent = item.name;
  li.querySelector('.sub').textContent = sub.join(' · ');
  li.querySelector('.thumb').addEventListener('dblclick', () => window.open(item.url, '_blank'));
  li.addEventListener('dragstart', (e) => startDrag(e, item, li));
  li.addEventListener('dragend', () => li.classList.remove('dragging'));
  li.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', (e) => { e.stopPropagation(); act(b.dataset.act, item); }));
  return li;
}

function mimeOf(item) {
  const ext = item.name.split('.').pop().toLowerCase();
  return { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', mp4: 'video/mp4', mkv: 'video/x-matroska', mov: 'video/quicktime', ts: 'video/mp2t' }[ext] || 'application/octet-stream';
}

function startDrag(e, item, li) {
  li.classList.add('dragging');
  // Inside the Hoard window the shell drags the real file (any program takes it).
  if (window.hoardWindow && typeof window.hoardWindow.startDrag === 'function') {
    e.preventDefault();
    window.hoardWindow.startDrag(item.path);
    setTimeout(() => li.classList.remove('dragging'), 300);
    return;
  }
  // In a browser: Chrome turns DownloadURL into a file for the drop target.
  const abs = new URL(item.url, location.href).href;
  e.dataTransfer.effectAllowed = 'copy';
  e.dataTransfer.setData('DownloadURL', `${mimeOf(item)}:${item.name}:${abs}`);
  e.dataTransfer.setData('text/uri-list', abs);
  e.dataTransfer.setData('text/plain', item.path);
  const img = li.querySelector('img');
  if (img && img.complete) e.dataTransfer.setDragImage(img, 40, 24);
}

async function act(what, item) {
  const enc = encodeURIComponent(item.name);
  try {
    if (what === 'copy') {
      await api(`/api/media/${enc}/copy`, { method: 'POST' });
      toast('Copiado. Pégalo en el chat con Ctrl+V.', 'ok');
    } else if (what === 'reveal') {
      await api(`/api/media/${enc}/reveal`, { method: 'POST' });
    } else if (what === 'light') {
      const job = await api(`/api/media/${enc}/light`, { method: 'POST' });
      (state.jobsStarted || (state.jobsStarted = new Set())).add(job.id);
      toast('Haciendo la copia ligera…');
    } else if (what === 'delete') {
      const r = await api(`/api/media/${enc}`, { method: 'DELETE' });
      toast(r.recycle_bin ? 'Enviado a la papelera.' : 'Borrado.');
      refreshList(true);
    }
  } catch (err) { fail(err); }
}

// ---------------------------------------------------------------- capture buttons

async function shot() {
  if ($('btn-shot').disabled) return;
  $('flash').classList.remove('go'); void $('flash').offsetWidth; $('flash').classList.add('go');
  try {
    const it = await api('/api/snapshot', { method: 'POST', body: {} });
    markFresh(it.name);
    await refreshList(true);
    toast('Captura guardada. Arrástrala o pulsa Copiar.', 'ok');
  } catch (err) { fail(err); }
}

async function clip() {
  if ($('btn-clip').disabled) return;
  $('btn-clip').disabled = true;
  try {
    const it = await api('/api/clip', { method: 'POST', body: {} });
    markFresh(it.name);
    await refreshList(true);
    toast(`Clip guardado (${fmtDur(it.duration)}).`, 'ok');
  } catch (err) { fail(err); } finally { $('btn-clip').disabled = false; }
}

async function record() {
  if ($('btn-rec').disabled) return;
  const recording = state.status && state.status.recording && state.status.recording.state === 'recording';
  try {
    await api(recording ? '/api/record/stop' : '/api/record/start', { method: 'POST' });
    if (!recording) toast('Grabando. Pulsa R o Parar para terminar.');
    refreshStatus();
  } catch (err) { fail(err); }
}

function toggleMute() {
  video.muted = !video.muted;
  $('btn-mute').textContent = video.muted ? '🔇' : '🔊';
  if (!video.muted) video.play().catch(() => {});
}

function toggleFull() {
  if (document.fullscreenElement) document.exitFullscreen();
  else $('stage').requestFullscreen().catch(() => {});
}

$('btn-shot').addEventListener('click', shot);
$('btn-clip').addEventListener('click', clip);
$('btn-rec').addEventListener('click', record);
$('btn-mute').addEventListener('click', toggleMute);
$('btn-full').addEventListener('click', toggleFull);
$('stage').addEventListener('dblclick', toggleFull);
$('btn-folder').addEventListener('click', () => api('/api/folder/reveal', { method: 'POST' }).catch(fail));

document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target && e.target.tagName) || '';
  if (/INPUT|SELECT|TEXTAREA/.test(tag) || $('settings').open) return;
  const k = e.key.toLowerCase();
  if (k === 'c') { shot(); e.preventDefault(); }
  else if (k === 'b') { clip(); e.preventDefault(); }
  else if (k === 'r') { record(); e.preventDefault(); }
  else if (k === 'm') { toggleMute(); e.preventDefault(); }
  else if (k === 'f') { toggleFull(); e.preventDefault(); }
});

$('tabs').addEventListener('click', (e) => {
  const b = e.target.closest('.tab');
  if (!b) return;
  state.kind = b.dataset.kind;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === b));
  refreshList(true);
});

// ---------------------------------------------------------------- settings

async function openSettings() {
  const form = $('settings-form');
  try {
    const [s, d] = await Promise.all([api('/api/settings'), api('/api/devices').catch(() => ({ devices: [] }))]);
    state.settings = s;
    const sel = $('sel-device');
    sel.textContent = '';
    const names = d.devices.length ? d.devices : [s.device];
    for (const n of names) {
      const o = document.createElement('option');
      const match = n.toLowerCase().includes((s.device || '').toLowerCase());
      o.value = n.match(/C\d{3}/) ? n.match(/C\d{3}/)[0] : n;
      o.textContent = n;
      if (match) o.selected = true;
      sel.appendChild(o);
    }
    form.capture_enabled.checked = s.capture_enabled;
    for (const k of ['audio_input', 'snapshot_format', 'clip_seconds', 'buffer_seconds', 'light_height', 'light_kbps']) {
      const el = form[k];
      if (![...el.options].some((o) => o.value === String(s[k]))) {
        const o = document.createElement('option'); o.value = o.textContent = String(s[k]); el.appendChild(o);
      }
      el.value = String(s[k]);
    }
    form.media_dir.value = s.media_dir || '';
    form.media_dir.placeholder = `Por defecto: ${s.media_dir_effective}`;
  } catch (err) { fail(err); return; }
  $('settings').showModal();
}

$('btn-settings').addEventListener('click', openSettings);
$('settings').addEventListener('close', async () => {
  if ($('settings').returnValue !== 'save') return;
  const f = $('settings-form');
  const body = {
    capture_enabled: f.capture_enabled.checked, device: f.device.value, audio_input: f.audio_input.value,
    snapshot_format: f.snapshot_format.value, clip_seconds: +f.clip_seconds.value, buffer_seconds: +f.buffer_seconds.value,
    media_dir: f.media_dir.value.trim(), light_height: +f.light_height.value, light_kbps: +f.light_kbps.value,
  };
  try { await api('/api/settings', { method: 'PUT', body }); toast('Ajustes guardados.', 'ok'); refreshStatus(); refreshList(true); }
  catch (err) { fail(err); }
});

// ---------------------------------------------------------------- start

refreshStatus();
refreshList(true);
setInterval(refreshStatus, 1000);
setInterval(() => refreshList(false), 4000);
