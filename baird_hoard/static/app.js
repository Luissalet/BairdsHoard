'use strict';
// Baird's Hoard UI: live picture (fragmented MP4 over a WebSocket into Media Source Extensions), the capture
// buttons (screenshot, zone, clip, record), the recent files (drag them out, copy them, annotate them, send them to
// Faustus), the text search, the screen-watch rules, picture-in-picture and the settings.

const $ = (id) => document.getElementById(id);
const video = $('video');
const state = {
  kind: 'all', items: [], fresh: new Set(), status: null, settings: null, lastListKey: '',
  search: '', lastEventId: undefined, hotkeyAt: undefined,
};

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
    this.retry = 1000; this.retryTimer = null;
    this.pageActive = true;
    setInterval(() => this.chase(), 400);
    document.addEventListener('visibilitychange', () => this.sync());
    el.addEventListener('enterpictureinpicture', () => this.sync());
    el.addEventListener('leavepictureinpicture', () => this.sync());
    window.addEventListener('pagehide', () => { this.pageActive = false; this.sync(); });
    window.addEventListener('pageshow', () => { this.pageActive = true; this.sync(); });
  }

  sync() {
    if (this.pageActive && (!document.hidden || document.pictureInPictureElement === this.el)) {
      this.connect();
      this.chase(true);
    } else {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
      const ws = this.ws;
      this.ws = null;
      if (ws) ws.close();
      this.teardown();
    }
  }

  connect() {
    if (this.ws || this.retryTimer) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/api/live`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => { this.retry = 1000; };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
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
      if (this.ws !== ws) return;
      this.ws = null;
      this.teardown();
      this.retryTimer = setTimeout(() => { this.retryTimer = null; this.sync(); }, this.retry);
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
player.sync();

// ---------------------------------------------------------------- status

const STATE_TEXT = {
  running: 'En directo', starting: 'Conectando…', no_signal: 'Sin señal', no_device: 'Sin capturadora',
  idle: 'En espera', rate_limited: 'Límite de aperturas', off: 'Captura apagada', error: 'Error', stuck: 'Bloqueada',
};
const STATE_HINT = {
  starting: ['Abriendo la capturadora…', ''],
  idle: ['Captura en espera', ''],
  rate_limited: ['Límite de aperturas alcanzado', 'Baird esperará antes de volver a abrir la capturadora.'],
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
  $('btn-retry').hidden = !['no_signal', 'no_device', 'stuck', 'error'].includes(s);
  $('stage-msg').hidden = s === 'running' || !msg;
  if (msg) {
    if ($('stage-msg-title').textContent !== msg[0]) $('stage-msg-title').textContent = msg[0];
    const hint = s === 'error' ? (cap.message || '') : msg[1];
    $('stage-msg-hint').textContent = cap.retry_in_s > 0 && s !== 'off'
      ? [hint, `Próximo intento en ${fmtDur(Math.ceil(cap.retry_in_s))}.`].filter(Boolean).join(' ')
      : hint;
  }
  const live = s === 'running';
  $('btn-shot').disabled = !live;
  $('btn-zone').disabled = !live;
  $('btn-clip').disabled = !live;
  $('clip-label').textContent = `Últimos ${st.clip_seconds} s`;
  trackWatchAndHotkeys(st);

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

$('btn-retry').addEventListener('click', async () => {
  try { await api('/api/capture/retry', { method: 'POST' }); await refreshStatus(); }
  catch (err) { fail(err); }
});

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
  const q = state.search.trim();
  try {
    data = q ? await api(`/api/search?q=${encodeURIComponent(q)}&limit=120`) : await api(`/api/media?kind=${state.kind}&limit=120`);
  } catch (err) {
    if (q) { $('search-note').hidden = false; $('search-note').textContent = err.message; }
    return;
  }
  const note = $('search-note');
  note.hidden = !q;
  if (q) note.textContent = `${data.items.length} captura(s) con «${q}»` + (data.unread ? ` · ${data.unread} sin leer todavía` : '');
  $('side-tip').hidden = !!q;
  const key = (q ? `q:${q}|` : '') + data.items.map((i) => `${i.name}|${i.size}|${i.modified}`).join(',');
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
      <div class="snip" hidden></div>
      <div class="acts">
        <button class="btn small" data-act="copy" title="Copiar el archivo (pega con Ctrl+V)">Copiar</button>
        <button class="btn small accent" data-act="faustus" title="Abrir un chat de Faustus con este archivo adjunto">Faustus</button>
        ${isVideo ? '' : '<button class="btn small" data-act="annotate" title="Dibujar flechas, recuadros y texto en una copia">Anotar</button>'}
        ${isVideo ? '' : '<button class="btn small" data-act="text" title="Texto que se lee en la captura (OCR)">Texto</button>'}
        <button class="btn small" data-act="reveal" title="Mostrar en el Explorador">Explorador</button>
        ${isVideo && item.origin !== 'light' ? '<button class="btn small" data-act="light" title="Copia de 720p para chats con límite de tamaño">Ligera</button>' : ''}
        <button class="btn small danger" data-act="delete" title="Enviar a la papelera">Borrar</button>
      </div>
    </div>`;
  li.querySelector('img').src = item.thumb;
  li.querySelector('.name').textContent = item.name;
  li.querySelector('.sub').textContent = sub.join(' · ');
  if (item.snippet) { const sn = li.querySelector('.snip'); sn.hidden = false; sn.textContent = item.snippet; }
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
    } else if (what === 'faustus') {
      await sendToFaustus(item);
    } else if (what === 'annotate') {
      openAnnotate(item);
    } else if (what === 'text') {
      await showText(item);
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
  if (/INPUT|SELECT|TEXTAREA/.test(tag) || document.querySelector('dialog[open]') || zonePicking) return;
  const k = e.key.toLowerCase();
  if (k === 'z') { zoneShot(); e.preventDefault(); }
  else if (k === 'p') { togglePip(); e.preventDefault(); }
  else if (k === 'c') { shot(); e.preventDefault(); }
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
    const s = await api('/api/settings');
    state.settings = s;
    showDevices([], s.device);
    form.capture_enabled.checked = s.capture_enabled;
    form.capture_always_on.checked = s.capture_always_on;
    for (const k of ['audio_input', 'snapshot_format', 'clip_seconds', 'buffer_seconds', 'light_height', 'light_kbps']) {
      const el = form[k];
      if (![...el.options].some((o) => o.value === String(s[k]))) {
        const o = document.createElement('option'); o.value = o.textContent = String(s[k]); el.appendChild(o);
      }
      el.value = String(s[k]);
    }
    form.media_dir.value = s.media_dir || '';
    form.media_dir.placeholder = `Por defecto: ${s.media_dir_effective}`;
    form.ocr_auto.checked = !!s.ocr_auto;
    form.hotkeys_enabled.checked = !!s.hotkeys_enabled;
    form.hotkey_shot.value = s.hotkey_shot || '';
    form.hotkey_clip.value = s.hotkey_clip || '';
    api('/api/hotkeys').then(describeHotkeys).catch(() => {});
  } catch (err) { fail(err); return; }
  $('settings').showModal();
}

function showDevices(names, current) {
  const sel = $('sel-device');
  sel.textContent = '';
  for (const name of names) {
    const option = document.createElement('option');
    option.value = name.match(/C\d{3}/)?.[0] || name;
    option.textContent = name;
    sel.appendChild(option);
  }
  if (![...sel.options].some((option) => option.value === current)) sel.add(new Option(current, current));
  sel.value = current;
}

$('btn-devices').addEventListener('click', async () => {
  try { const data = await api('/api/devices'); showDevices(data.devices, $('sel-device').value); }
  catch (err) { fail(err); }
});

$('btn-settings').addEventListener('click', openSettings);
$('settings').addEventListener('close', async () => {
  if ($('settings').returnValue !== 'save') return;
  const f = $('settings-form');
  const body = {
    capture_enabled: f.capture_enabled.checked, capture_always_on: f.capture_always_on.checked, device: f.device.value, audio_input: f.audio_input.value,
    snapshot_format: f.snapshot_format.value, clip_seconds: +f.clip_seconds.value, buffer_seconds: +f.buffer_seconds.value,
    media_dir: f.media_dir.value.trim(), light_height: +f.light_height.value, light_kbps: +f.light_kbps.value,
    ocr_auto: f.ocr_auto.checked, hotkeys_enabled: f.hotkeys_enabled.checked,
    hotkey_shot: f.hotkey_shot.value.trim(), hotkey_clip: f.hotkey_clip.value.trim(),
  };
  try { await api('/api/settings', { method: 'PUT', body }); toast('Ajustes guardados.', 'ok'); refreshStatus(); refreshList(true); }
  catch (err) { fail(err); }
});

// ---------------------------------------------------------------- zone picking (capture of one area)

let zonePicking = false;

// Where the picture really is inside the stage (the video is letter-boxed with object-fit: contain).
function videoBox() {
  const st = $('stage').getBoundingClientRect();
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return { left: 0, top: 0, width: st.width, height: st.height };
  const k = Math.min(st.width / vw, st.height / vh);
  const w = vw * k, h = vh * k;
  return { left: (st.width - w) / 2, top: (st.height - h) / 2, width: w, height: h };
}

// Drag a rectangle over the live picture. Resolves {x, y, w, h, unit: 'fraction'} (0..1 of the picture) or null.
function pickRegion(hint) {
  return new Promise((resolve) => {
    if (zonePicking) { resolve(null); return; }
    zonePicking = true;
    const layer = $('zone-layer'), box = $('zone-box');
    $('zone-hint').textContent = hint || 'Arrastra sobre la imagen para elegir la zona · Esc cancela';
    layer.hidden = false; box.hidden = true;
    let a = null, vb = null;
    const clampX = (x) => Math.min(vb.left + vb.width, Math.max(vb.left, x));
    const clampY = (y) => Math.min(vb.top + vb.height, Math.max(vb.top, y));
    const rel = (e) => { const r = $('stage').getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    const done = (region) => {
      zonePicking = false; layer.hidden = true; box.hidden = true;
      layer.removeEventListener('pointerdown', down); layer.removeEventListener('pointermove', move);
      layer.removeEventListener('pointerup', up); document.removeEventListener('keydown', key, true);
      resolve(region);
    };
    const draw = (b) => {
      box.hidden = false;
      Object.assign(box.style, { left: `${b.x}px`, top: `${b.y}px`, width: `${b.w}px`, height: `${b.h}px` });
    };
    const rect = (e) => {
      const [px, py] = rel(e); const x = clampX(px), y = clampY(py);
      return { x: Math.min(a[0], x), y: Math.min(a[1], y), w: Math.abs(x - a[0]), h: Math.abs(y - a[1]) };
    };
    const down = (e) => {
      vb = videoBox(); const [px, py] = rel(e);
      a = [clampX(px), clampY(py)]; layer.setPointerCapture(e.pointerId); draw({ x: a[0], y: a[1], w: 0, h: 0 }); e.preventDefault();
    };
    const move = (e) => { if (a) draw(rect(e)); };
    const up = (e) => {
      if (!a) return;
      const b = rect(e); a = null;
      if (b.w < 10 || b.h < 10) { toast('La zona es demasiado pequeña.', 'err'); done(null); return; }
      done({ x: (b.x - vb.left) / vb.width, y: (b.y - vb.top) / vb.height, w: b.w / vb.width, h: b.h / vb.height, unit: 'fraction' });
    };
    const key = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); } };
    layer.addEventListener('pointerdown', down); layer.addEventListener('pointermove', move); layer.addEventListener('pointerup', up);
    document.addEventListener('keydown', key, true);
  });
}

async function zoneShot() {
  if ($('btn-zone').disabled || zonePicking) return;
  const region = await pickRegion();
  if (!region) return;
  $('flash').classList.remove('go'); void $('flash').offsetWidth; $('flash').classList.add('go');
  try {
    const it = await api('/api/snapshot', { method: 'POST', body: { region } });
    markFresh(it.name);
    await refreshList(true);
    toast(`Zona guardada (${it.width}×${it.height}).`, 'ok');
  } catch (err) { fail(err); }
}
$('btn-zone').addEventListener('click', zoneShot);

// ---------------------------------------------------------------- picture-in-picture

async function togglePip() {
  if (!document.pictureInPictureEnabled || video.disablePictureInPicture) {
    toast('Este navegador no permite la vista mini. Prueba con Chrome o Edge.', 'err');
    return;
  }
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else {
      if (video.readyState < 2) throw new Error('Todavía no hay imagen.');
      await video.requestPictureInPicture();
    }
  } catch (err) { toast(`No se pudo abrir la vista mini: ${err.message}`, 'err'); }
}
video.addEventListener('enterpictureinpicture', () => $('btn-pip').classList.add('on'));
video.addEventListener('leavepictureinpicture', () => $('btn-pip').classList.remove('on'));
$('btn-pip').addEventListener('click', togglePip);

// ---------------------------------------------------------------- search by the text of the screenshots

let searchTimer = null;
$('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = $('search').value; state.lastListKey = ''; refreshList(true); }, 300);
});
$('search').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { $('search').value = ''; state.search = ''; refreshList(true); }
});

async function showText(item) {
  toast('Leyendo el texto…');
  const r = await api(`/api/media/${encodeURIComponent(item.name)}/ocr`, { method: 'POST' });
  $('textview-title').textContent = `Texto de ${item.name}`;
  $('textview-body').value = r.text || '(No se lee texto en esta captura.)';
  $('textview').showModal();
}
$('textview-close').addEventListener('click', () => $('textview').close());
$('textview-copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('textview-body').value); toast('Texto copiado.', 'ok'); }
  catch { $('textview-body').select(); document.execCommand('copy'); }
});

// ---------------------------------------------------------------- send to Faustus

async function sendToFaustus(item) {
  toast(`Enviando ${item.name} a Faustus…`);
  const r = await api(`/api/media/${encodeURIComponent(item.name)}/faustus`, { method: 'POST', body: { message: '' } });
  const el = document.createElement('div');
  el.className = 'toast ok';
  const link = document.createElement('a');
  link.href = r.url; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Abrir el chat en Faustus';
  el.append('Chat creado con el archivo adjunto. ', link);
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 12000);
  window.open(r.url, '_blank', 'noopener');
  return r;
}

// ---------------------------------------------------------------- annotate

const ann = { item: null, img: null, tool: 'arrow', color: '#ff3b30', shapes: [], draft: null };

function openAnnotate(item) {
  const img = new Image();
  img.onload = () => {
    ann.item = item; ann.img = img; ann.shapes = []; ann.draft = null;
    const c = $('ann-canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    $('ann-text').hidden = true;
    $('annotate').showModal();
    drawAnn();
  };
  img.onerror = () => toast('No se pudo cargar la captura.', 'err');
  img.src = `${item.url}?t=${Date.now()}`;
}

function annSizes() {
  const w = $('ann-canvas').width;
  return { lw: Math.max(3, Math.round(w / 320)), fs: Math.max(20, Math.round(w / 38)) };
}

function drawShape(ctx, s, sizes) {
  ctx.strokeStyle = s.color; ctx.fillStyle = s.color; ctx.lineWidth = sizes.lw; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  if (s.type === 'rect') {
    ctx.strokeRect(Math.min(s.x1, s.x2), Math.min(s.y1, s.y2), Math.abs(s.x2 - s.x1), Math.abs(s.y2 - s.y1));
  } else if (s.type === 'arrow') {
    const ang = Math.atan2(s.y2 - s.y1, s.x2 - s.x1), head = sizes.lw * 5;
    ctx.beginPath(); ctx.moveTo(s.x1, s.y1); ctx.lineTo(s.x2, s.y2); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(s.x2, s.y2);
    ctx.lineTo(s.x2 - head * Math.cos(ang - Math.PI / 6), s.y2 - head * Math.sin(ang - Math.PI / 6));
    ctx.lineTo(s.x2 - head * Math.cos(ang + Math.PI / 6), s.y2 - head * Math.sin(ang + Math.PI / 6));
    ctx.closePath(); ctx.fill();
  } else if (s.type === 'text') {
    ctx.font = `700 ${sizes.fs}px "Segoe UI", system-ui, sans-serif`; ctx.textBaseline = 'top';
    ctx.lineWidth = Math.max(3, sizes.fs / 6); ctx.strokeStyle = 'rgba(0,0,0,.85)';
    ctx.strokeText(s.text, s.x1, s.y1); ctx.fillText(s.text, s.x1, s.y1);
  }
}

function drawAnn() {
  const c = $('ann-canvas'), ctx = c.getContext('2d'), sizes = annSizes();
  ctx.clearRect(0, 0, c.width, c.height);
  if (ann.img) ctx.drawImage(ann.img, 0, 0);
  for (const s of ann.shapes) drawShape(ctx, s, sizes);
  if (ann.draft) drawShape(ctx, ann.draft, sizes);
}

function annPoint(e) {
  const c = $('ann-canvas'), r = c.getBoundingClientRect();
  return [(e.clientX - r.left) * c.width / r.width, (e.clientY - r.top) * c.height / r.height];
}

(() => {
  const c = $('ann-canvas');
  c.addEventListener('pointerdown', (e) => {
    const [x, y] = annPoint(e);
    if (ann.tool === 'text') {
      const input = $('ann-text'), stage = $('ann-stage').getBoundingClientRect();
      input.hidden = false; input.value = '';
      input.style.left = `${e.clientX - stage.left}px`; input.style.top = `${e.clientY - stage.top}px`;
      input.dataset.x = x; input.dataset.y = y;
      setTimeout(() => input.focus(), 0);
      e.preventDefault();
      return;
    }
    ann.draft = { type: ann.tool, color: ann.color, x1: x, y1: y, x2: x, y2: y };
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener('pointermove', (e) => {
    if (!ann.draft) return;
    [ann.draft.x2, ann.draft.y2] = annPoint(e); drawAnn();
  });
  c.addEventListener('pointerup', () => {
    if (!ann.draft) return;
    const d = ann.draft; ann.draft = null;
    if (Math.hypot(d.x2 - d.x1, d.y2 - d.y1) > 6) ann.shapes.push(d);
    drawAnn();
  });
  const input = $('ann-text');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && input.value.trim()) {
      ann.shapes.push({ type: 'text', color: ann.color, text: input.value.trim(), x1: +input.dataset.x, y1: +input.dataset.y });
      input.hidden = true; drawAnn();
    } else if (e.key === 'Escape') { e.stopPropagation(); input.hidden = true; }
  });
})();

$('ann-tools').addEventListener('click', (e) => {
  const b = e.target.closest('[data-tool]'); if (!b) return;
  ann.tool = b.dataset.tool; $('ann-text').hidden = true;
  document.querySelectorAll('#ann-tools [data-tool]').forEach((x) => x.classList.toggle('active', x === b));
});
$('ann-colors').addEventListener('click', (e) => {
  const b = e.target.closest('[data-color]'); if (!b) return;
  ann.color = b.dataset.color;
  document.querySelectorAll('#ann-colors [data-color]').forEach((x) => x.classList.toggle('active', x === b));
});
$('ann-undo').addEventListener('click', () => { ann.shapes.pop(); drawAnn(); });
$('ann-clear').addEventListener('click', () => { ann.shapes = []; drawAnn(); });
$('ann-cancel').addEventListener('click', () => $('annotate').close());

async function saveAnnotated(thenSend) {
  if (!ann.item) return;
  $('ann-text').hidden = true; ann.draft = null; drawAnn();
  try {
    const data = $('ann-canvas').toDataURL('image/png');
    const it = await api(`/api/media/${encodeURIComponent(ann.item.name)}/annotate`, { method: 'POST', body: { data } });
    $('annotate').close();
    markFresh(it.name);
    await refreshList(true);
    toast(`Copia anotada guardada: ${it.name}`, 'ok');
    if (thenSend) await sendToFaustus(it);
  } catch (err) { fail(err); }
}
$('ann-save').addEventListener('click', () => saveAnnotated(false));
$('ann-save-send').addEventListener('click', () => saveAnnotated(true));

// ---------------------------------------------------------------- watch the screen

const watchForm = { region: null };

function describeRule(r) {
  const zone = r.region ? `zona de ${Math.round(r.region.w * 100)}×${Math.round(r.region.h * 100)} % de la pantalla` : 'toda la pantalla';
  if (r.kind === 'text') return `${r.mode === 'appears' ? 'Aparece' : 'Desaparece'} «${r.text}» · ${zone} · cada ${r.interval_s} s`;
  if (r.mode === 'still') return `Deja de moverse ${r.still_seconds} s (umbral ${r.threshold} %) · ${zone}`;
  return `Cambia más de ${r.threshold} % · ${zone} · cada ${r.interval_s} s`;
}

function ruleStatus(r) {
  const s = r.status || {};
  if (s.error) return s.error;
  if (!r.enabled) return 'En pausa';
  if (s.value == null) return s.state || 'Esperando la primera mirada';
  return r.kind === 'text' ? (s.value ? 'El texto se ve ahora' : 'El texto no se ve') : `Último cambio: ${s.value} %`;
}

async function loadWatch() {
  const data = await api('/api/watch');
  const ul = $('watch-rules'); ul.textContent = '';
  $('watch-empty').hidden = data.rules.length > 0;
  for (const r of data.rules) {
    const li = document.createElement('li');
    li.className = 'rule' + (r.enabled ? '' : ' off');
    li.innerHTML = '<div class="rule-main"><div class="rule-name"></div><div class="rule-desc"></div><div class="rule-state"></div></div><div class="rule-acts"></div>';
    li.querySelector('.rule-name').textContent = r.name;
    li.querySelector('.rule-desc').textContent = describeRule(r);
    li.querySelector('.rule-state').textContent = ruleStatus(r);
    const acts = li.querySelector('.rule-acts');
    for (const [label, fn] of [
      [r.enabled ? 'Pausar' : 'Activar', async () => { await api(`/api/watch/${r.id}`, { method: 'PUT', body: { enabled: !r.enabled } }); }],
      ['Mirar ahora', async () => {
        const res = await api(`/api/watch/${r.id}/check`, { method: 'POST' });
        toast(res.error ? res.error : res.fired ? `Se cumplió: ${res.detail}` : `Sin novedad (${res.value == null ? 'primera mirada' : res.value}).`, res.error ? 'err' : '');
      }],
      ['Borrar', async () => { await api(`/api/watch/${r.id}`, { method: 'DELETE' }); }],
    ]) {
      const b = document.createElement('button');
      b.className = 'btn small' + (label === 'Borrar' ? ' danger' : ''); b.textContent = label;
      b.addEventListener('click', async () => { try { await fn(); await loadWatch(); refreshStatus(); } catch (err) { fail(err); } });
      acts.appendChild(b);
    }
    ul.appendChild(li);
  }
  const ev = $('watch-events'); ev.textContent = '';
  $('watch-events-empty').hidden = data.events.length > 0;
  for (const e of data.events) {
    const li = document.createElement('li');
    li.innerHTML = '<span class="ev-time"></span><span class="ev-text"></span>';
    li.querySelector('.ev-time').textContent = fmtTime(new Date(e.at * 1000).toISOString());
    li.querySelector('.ev-text').textContent = `${e.rule}: ${e.detail}` + (e.notified === true ? ' · avisado' : '');
    ev.appendChild(li);
  }
}

function syncWatchForm() {
  const [kind, mode] = $('wf-type').value.split(':');
  $('wf-row-threshold').hidden = kind !== 'change';
  $('wf-row-still').hidden = mode !== 'still';
  $('wf-row-text').hidden = kind !== 'text';
  $('wf-zone-state').textContent = watchForm.region
    ? `${Math.round(watchForm.region.w * 100)}×${Math.round(watchForm.region.h * 100)} % de la pantalla` : 'Toda la pantalla';
}

async function openWatch() {
  try { await loadWatch(); } catch (err) { fail(err); return; }
  syncWatchForm();
  if (!$('watch').open) $('watch').showModal();
}

$('btn-watch').addEventListener('click', openWatch);
$('watch-close').addEventListener('click', () => $('watch').close());
$('wf-type').addEventListener('change', syncWatchForm);
$('wf-all').addEventListener('click', () => { watchForm.region = null; syncWatchForm(); });
$('wf-pick').addEventListener('click', async () => {
  $('watch').close();
  const region = await pickRegion('Arrastra sobre la imagen la zona que vigilar · Esc cancela');
  if (region) watchForm.region = region;
  await openWatch();
});
$('wf-add').addEventListener('click', async () => {
  const [kind, mode] = $('wf-type').value.split(':');
  const body = { kind, mode, cooldown_s: +$('wf-cooldown').value };
  if ($('wf-name').value.trim()) body.name = $('wf-name').value.trim();
  if (watchForm.region) body.region = watchForm.region;
  if (kind === 'change') body.threshold = +$('wf-threshold').value;
  if (mode === 'still') body.still_seconds = +$('wf-still').value;
  if (kind === 'text') body.text = $('wf-text').value.trim();
  try {
    await api('/api/watch', { method: 'POST', body });
    toast('Regla añadida.', 'ok');
    $('wf-name').value = ''; $('wf-text').value = '';
    await loadWatch(); refreshStatus();
  } catch (err) { fail(err); }
});

function trackWatchAndHotkeys(st) {
  const w = st.watch || {};
  const cnt = $('watch-count');
  cnt.hidden = !w.active; cnt.textContent = w.active || '';
  const ev = w.last_event;
  if (state.lastEventId === undefined) state.lastEventId = ev ? ev.id : null;
  else if (ev && ev.id !== state.lastEventId) {
    state.lastEventId = ev.id;
    toast(`Aviso: ${ev.rule}. ${ev.detail}`, 'ok');
    if (ev.image) markFresh(ev.image);
    refreshList(true);
    if ($('watch').open) loadWatch().catch(() => {});
  }
  const last = (st.hotkeys || {}).last;
  if (state.hotkeyAt === undefined) state.hotkeyAt = last ? last.at : null;
  else if (last && last.at !== state.hotkeyAt) {
    state.hotkeyAt = last.at;
    if (last.ok) { toast(`${last.label} guardada con el atajo.`, 'ok'); markFresh(last.name); }
    else toast(`El atajo no pudo guardar: ${last.error}`, 'err');
    refreshList(true);
  }
}

function describeHotkeys(hk) {
  const note = $('hotkey-note');
  if (!hk || !hk.enabled) { note.textContent = 'Los atajos globales están desactivados.'; return; }
  if (hk.error) { note.textContent = hk.error; return; }
  const parts = Object.entries(hk.registered || {}).map(([k, v]) =>
    `${k === 'shot' ? 'Captura' : 'Clip'} ${v.combo}: ${v.ok ? 'activo' : v.error}`);
  note.textContent = hk.running ? (parts.join(' · ') || 'Arrancando el ayudante…') : 'El ayudante de atajos no está en marcha (se arranca con la app).';
}

// ---------------------------------------------------------------- start

refreshStatus();
refreshList(true);
setInterval(refreshStatus, 1000);
setInterval(() => refreshList(false), 4000);
