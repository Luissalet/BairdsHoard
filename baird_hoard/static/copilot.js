'use strict';
// Copilot: the marks an assistant draws over the live picture, the bar under the picture that carries its instruction
// (the subtitle) and the person's buttons, the conversation beside it, and the top-bar button that hands the copilot's
// address to a chat. The state arrives over /copilot/ws; everything the person does goes to POST /copilot/user.
// A server without the copilot leaves all of this hidden.
(() => {
  const stage = document.getElementById('stage');
  const opener = document.getElementById('btn-copilot');
  if (!stage || !opener) return;
  const ADDRESS = `${location.origin}/copilot`;
  const PHRASE = `Lee ${ADDRESS} y guíame en directo en Baird: `;
  const SVG = 'http://www.w3.org/2000/svg';
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const note = (text, kind) => { if (typeof toast === 'function') toast(text, kind); };
  const keep = {
    get(key) { try { return localStorage.getItem(`baird-copilot-${key}`); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(`baird-copilot-${key}`, value); } catch { /* private mode */ } },
  };
  // place: where the bar sits - under the picture ("dock", the default) or inside it at the bottom or the top.
  const ui = { state: null, ws: null, retry: 1000, timer: null, asking: false, listOpen: false, panel: null, fails: 0,
    place: ['dock', 'bottom', 'top'].includes(keep.get('bar')) ? keep.get('bar') : 'dock', voice: keep.get('voice') === '1',
    heard: null, logKey: '' };

  const layer = el('div', 'cp-layer');
  const svg = document.createElementNS(SVG, 'svg');
  layer.appendChild(svg);
  const hud = el('div', 'cp-hud');
  const dock = el('div', 'cp-dock');              // under the picture: the subtitle never covers the screen being watched
  const talk = el('section', 'cp-talk');          // beside it: what the copilot says and what the person answers
  layer.hidden = hud.hidden = dock.hidden = talk.hidden = true;
  stage.append(layer);
  stage.after(dock);
  dock.append(hud);
  const side = document.querySelector('.side');
  if (side) side.prepend(talk);
  for (const type of ['dblclick', 'pointerdown']) hud.addEventListener(type, (e) => e.stopPropagation());
  const drawn = new Map();        // mark key -> element, so a mark that moves glides instead of blinking

  // ---------------------------------------------------------------- talking to the app

  async function call(path, body, method) {
    const res = await fetch(path, { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error([data.error, data.hint].filter(Boolean).join(' ') || `HTTP ${res.status}`);
    return data;
  }

  async function act(action, text) {
    try { apply(await call('/copilot/user', { action, text: text || '' })); }
    catch (err) { note(err.message, 'err'); }
  }

  async function load() {
    try {
      const res = await fetch('/copilot/state', { cache: 'no-store' });
      if (!res.ok || !(res.headers.get('content-type') || '').includes('json')) return false;
      apply(await res.json());
      return true;
    } catch { return false; }
  }

  function listen() {
    if (ui.ws || document.hidden) return;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/copilot/ws`);
    ui.ws = ws;
    ws.onopen = () => { ui.retry = 1000; ui.fails = 0; clearInterval(ui.timer); ui.timer = null; };
    ws.onmessage = (ev) => { try { apply(JSON.parse(ev.data)); } catch { /* not a state */ } };
    ws.onclose = () => {
      if (ui.ws !== ws) return;
      ui.ws = null;
      ui.fails += 1;
      if (ui.fails >= 3 && !ui.timer) ui.timer = setInterval(load, 1000);     // no socket here: ask every second instead
      setTimeout(listen, ui.retry);
      ui.retry = Math.min(ui.retry * 1.6, 8000);
    };
  }

  function apply(state) {
    if (!state || typeof state !== 'object' || !('session' in state)) return;
    if (ui.state && state.rev < ui.state.rev && state.session && ui.state.session && state.session.id === ui.state.session.id) return;
    const before = ui.state && ui.state.session;
    ui.state = state;
    if (!state.session || !before || before.id !== state.session.id || before.index !== state.session.index) closeAsk();
    render();
  }

  // ---------------------------------------------------------------- where the picture is

  function pictureBox() {
    if (typeof videoBox === 'function') return videoBox();
    return { left: 0, top: 0, width: stage.clientWidth, height: stage.clientHeight };
  }

  function layout() {
    const b = pictureBox();
    Object.assign(layer.style, { left: `${b.left}px`, top: `${b.top}px`, width: `${b.width}px`, height: `${b.height}px` });
    const unit = Math.max(9, Math.min(17, b.width / 95));
    layer.style.setProperty('--cp-u', `${unit}px`);
    svg.setAttribute('viewBox', `0 0 ${Math.max(1, b.width)} ${Math.max(1, b.height)}`);
    return { ...b, unit };
  }

  // ---------------------------------------------------------------- marks

  const pct = (v) => `${(v * 100).toFixed(3)}%`;

  function tagSide(mark) {
    if (mark.x > 0.7) return 'cp-left';
    return '';
  }

  function pointMark(mark, opts) {
    const node = el('div', 'cp-mark');
    if (opts.rings) node.append(el('span', 'cp-ring'), el('span', 'cp-ring'));
    if (opts.dot) node.append(el('span', 'cp-dot'));
    if (opts.number) node.append(el('span', 'cp-num', String(opts.number)));
    if (opts.tag) {
      const tag = el('span', `cp-tag${opts.type ? ' cp-type' : ''}`, opts.tag);
      if (opts.type) {
        tag.title = 'Copiar este texto';
        tag.addEventListener('click', () => copy(opts.tag, 'Texto copiado.'));
      }
      node.append(tag);
    }
    return node;
  }

  function build(mark, number) {
    const k = mark.kind;
    if (k === 'box') {
      const node = el('div', 'cp-box');
      if (mark.text) node.append(el('span', 'cp-tag', mark.text));
      return node;
    }
    if (k === 'type') {
      const wrap = el('div', 'cp-group');
      if (mark.box) wrap.append(el('div', 'cp-box cp-field'));
      wrap.append(pointMark(mark, { dot: !mark.box, tag: mark.text, type: true }));
      return wrap;
    }
    if (k === 'note') return pointMark(mark, { tag: mark.text });
    if (k === 'arrow') return pointMark(mark, { dot: true, tag: mark.text, number });
    const tag = mark.text || (k === 'dbl' ? 'Doble clic' : k === 'right' ? 'Clic derecho' : '');
    return pointMark(mark, { rings: true, dot: true, tag, number });
  }

  function place(node, mark) {
    const point = node.classList.contains('cp-mark') ? node : node.querySelector('.cp-mark');
    const box = node.classList.contains('cp-box') ? node : node.querySelector('.cp-box');
    if (box && mark.box) {
      Object.assign(box.style, { left: pct(mark.box[0]), top: pct(mark.box[1]), width: pct(mark.box[2]), height: pct(mark.box[3]) });
      box.classList.toggle('cp-low', mark.box[1] < 0.07);
    }
    if (point && mark.x != null) {
      let x = mark.x, y = mark.y;
      point.classList.remove('cp-left', 'cp-under', 'cp-over');
      if (mark.kind === 'type' && mark.box) {           // the text to type hangs under its field (over it at the bottom of the screen)
        const over = mark.box[1] + mark.box[3] > 0.88;
        x = mark.box[0];
        y = over ? mark.box[1] : mark.box[1] + mark.box[3];
        point.classList.add(over ? 'cp-over' : 'cp-under');
      } else {
        const side = tagSide(mark);
        if (side) point.classList.add(side);
      }
      Object.assign(point.style, { left: pct(x), top: pct(y) });
    }
    node.classList.toggle('cp-lost', mark.state === 'lost');
    for (const part of node.querySelectorAll('.cp-mark')) part.classList.toggle('cp-lost', mark.state === 'lost');
  }

  function arrows(marks, box) {
    for (const old of svg.querySelectorAll('g')) old.remove();
    for (const mark of marks) {
      if (mark.kind !== 'arrow' || mark.x == null || !mark.to) continue;
      const x1 = mark.x * box.width, y1 = mark.y * box.height, x2 = mark.to[0] * box.width, y2 = mark.to[1] * box.height;
      const ang = Math.atan2(y2 - y1, x2 - x1), head = box.unit * 1.9, wide = box.unit * 0.42;
      const ex = x2 - Math.cos(ang) * head * 0.8, ey = y2 - Math.sin(ang) * head * 0.8;
      const g = document.createElementNS(SVG, 'g');
      if (mark.state === 'lost') g.setAttribute('opacity', '0.4');
      const line = (cls, width) => {
        const p = document.createElementNS(SVG, 'path');
        p.setAttribute('d', `M${x1} ${y1} L${ex} ${ey}`);
        p.setAttribute('class', cls);
        p.setAttribute('stroke-width', width);
        if (cls === 'cp-arrow') p.setAttribute('stroke-dasharray', `${wide * 2.4} ${wide * 1.7}`);
        return p;
      };
      const tip = document.createElementNS(SVG, 'path');
      const left = ang + Math.PI * 0.83, right = ang - Math.PI * 0.83;
      tip.setAttribute('d', `M${x2} ${y2} L${x2 + Math.cos(left) * head} ${y2 + Math.sin(left) * head} L${x2 + Math.cos(right) * head} ${y2 + Math.sin(right) * head} Z`);
      tip.setAttribute('class', 'cp-arrow-head');
      tip.setAttribute('stroke-width', wide * 0.5);
      g.append(line('cp-arrow-edge', wide * 1.9), line('cp-arrow', wide), tip);
      svg.appendChild(g);
    }
  }

  function renderMarks(session, box) {
    const step = session && session.status === 'active' ? session.step : null;
    const marks = step ? step.marks.filter((m) => m.state !== 'searching' && (m.x != null || m.box)) : [];
    const actions = marks.filter((m) => ['click', 'dbl', 'right', 'arrow'].includes(m.kind));
    const seen = new Set();
    for (const mark of marks) {
      const key = `${session.id}:${step.n}:${mark.id}:${mark.kind}:${mark.text}:${mark.box ? 1 : 0}`;
      seen.add(key);
      let node = drawn.get(key);
      if (!node) {
        node = build(mark, actions.length > 1 ? actions.indexOf(mark) + 1 || 0 : 0);
        drawn.set(key, node);
        layer.appendChild(node);
      }
      place(node, mark);
    }
    for (const [key, node] of drawn) if (!seen.has(key)) { node.remove(); drawn.delete(key); }
    arrows(marks, box);
    layer.hidden = marks.length === 0;
  }

  // ---------------------------------------------------------------- the bar

  function modeLine(session, step) {
    const searching = step.marks.filter((m) => m.state === 'searching' && m.find).map((m) => `«${m.find}»`);
    const lost = step.marks.filter((m) => m.state === 'lost' && m.find).map((m) => `«${m.find}»`);
    if (session.asked_help) {
      return session.listening ? 'Ayuda pedida: el copilot está mirando…' : 'Ayuda pedida, pero el copilot no está escuchando: avísale en su chat.';
    }
    if (searching.length) return `Buscando ${searching.join(', ')} en pantalla…`;
    if (lost.length) return `Ya no veo ${lost.join(', ')}: la marca sigue donde estaba.`;
    if (!step.auto) return 'Pulsa Hecho cuando termines.';
    return { change: 'Avanza solo cuando lo hagas.', typed: 'Avanza solo cuando lo escribas.', text: `Avanza cuando aparezca «${step.until_text}».`,
      gone: `Avanza cuando desaparezca «${step.until_text}».`, after: 'Avanza solo en un momento.' }[step.mode] || '';
  }

  function button(label, title, fn, cls, key) {
    const b = el('button', cls || '', label);
    b.type = 'button';
    b.title = title;
    if (key) b.append(el('kbd', '', key));
    b.addEventListener('click', (e) => { e.currentTarget.blur(); fn(); });
    return b;
  }

  // The box the person writes in is one and the same between renders (the bar is redrawn on every change of state):
  // what is half typed, and the cursor, stay.
  let ask = null;

  function closeAsk() {
    ui.asking = false;
    ask = null;
  }

  function askBox(session) {
    if (!ask) {
      const form = el('form', 'cp-ask');
      const input = el('input');
      input.type = 'text';
      input.maxLength = 300;
      input.autocomplete = 'off';
      form.append(input, button('Enviar', 'Enviar al copilot', () => form.requestSubmit()));
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        const now = ui.state && ui.state.session;
        const over = !now || now.status === 'done';
        const text = input.value.trim();
        if (over && !text) return;
        closeAsk();
        act(over ? 'say' : 'help', text);
      });
      input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Escape') { closeAsk(); render(); }
      });
      ask = { form, input, fresh: true };
    }
    ask.input.placeholder = session.status === 'done' ? 'Dile algo al copilot…' : '¿Qué pasa? (opcional) · Intro envía';
    return ask.form;
  }

  function inside() {
    return ui.place !== 'dock' || document.fullscreenElement === stage;       // full screen shows nothing but the picture
  }

  function askHelp() {
    const session = ui.state && ui.state.session;
    if (!session || session.status === 'stopped') return;
    if (document.fullscreenElement === stage || !side) {          // no conversation in sight: ask in the bar
      if (ui.asking) closeAsk(); else ui.asking = true;
      render();
      return;
    }
    if (session.status === 'active' && !session.asked_help) act('help');
    replyInput.focus();
  }

  function renderHud(session) {
    const typing = !!ask && document.activeElement === ask.input;
    hud.textContent = '';
    hud.hidden = !session;
    const within = inside();
    if (hud.parentElement !== (within ? stage : dock)) (within ? stage : dock).append(hud);
    hud.classList.toggle('cp-docked', !within);
    dock.hidden = !session || within;
    if (!session) return;
    const step = session.status === 'active' ? session.step : null;
    const who = el('div', 'cp-who');
    who.dataset.listening = session.listening ? '1' : '0';
    who.title = (session.goal ? `${session.goal}\n` : '') + (session.listening ? 'El copilot está atento.' : 'El copilot no está escuchando ahora.') + '\nPulsa para ver los pasos.';
    who.append(el('b', '', session.status === 'active' ? `${session.index + 1}/${session.count}` : session.status !== 'done' ? '■' : session.count ? '✓' : '…'));
    const name = el('span');
    name.append(el('i', 'cp-ear'), session.agent || 'Copilot');
    who.append(name);
    who.style.cursor = 'pointer';
    who.addEventListener('click', () => { ui.listOpen = !ui.listOpen; render(); });
    const body = el('div', 'cp-body');
    body.setAttribute('role', 'status');
    const acts = el('div', 'cp-acts');
    hud.classList.toggle('cp-ended', session.status === 'stopped');
    const last = session.log[session.log.length - 1];
    const said = last && last.who === 'agent' ? last.text : '';
    if (step) {
      if (said) body.append(el('div', 'cp-said', said));
      body.append(el('div', 'cp-say', step.say));
      if (step.hint) body.append(el('div', 'cp-hint', step.hint));
      const extra = el('div', 'cp-extra');
      for (const [i, combo] of step.keys.entries()) {
        if (i) extra.append(el('span', 'cp-mode', 'y después'));
        const keys = el('span', 'cp-keys');
        combo.forEach((key, n) => { if (n) keys.append(el('i', '', '+')); keys.append(el('kbd', '', key)); });
        extra.append(keys);
      }
      for (const mark of step.marks) {
        if (mark.kind !== 'type' || !mark.text) continue;
        const chip = el('span', 'cp-text', mark.text);
        chip.append(button('Copiar', 'Copiar este texto al portapapeles de este equipo', () => copy(mark.text, 'Texto copiado.')));
        extra.append(chip);
      }
      if (extra.childNodes.length) body.append(extra);
      const mode = modeLine(session, step);
      if (mode) body.append(el('div', 'cp-mode', mode));
      if (session.index > 0) acts.append(button('←', 'Volver al paso anterior (←)', () => act('back')));
      acts.append(button('Hecho', 'Ya está: siguiente paso (Espacio)', () => act('next'), 'cp-next', '␣'));
      const help = button('Ayuda', 'Avisar al copilot de que no te sale (H)', askHelp);
      help.dataset.on = session.asked_help ? '1' : '0';
      acts.append(help);
    } else if (session.status === 'done') {
      body.append(el('div', 'cp-say', said || (session.count ? 'Hecho.' : 'El copilot está contigo.')));
      const waiting = session.count ? 'El copilot está mirando cómo ha quedado…' : 'El copilot está preparando los pasos…';
      body.append(el('div', 'cp-mode', session.listening ? waiting : 'El copilot no está escuchando: dile en su chat que siga.'));
      if (session.count) acts.append(button('←', 'Volver al último paso (←)', () => act('back')));
      acts.append(button('Decirle algo', 'Escribirle una línea al copilot (H)', askHelp));
    } else {
      const why = { user: 'Guía parada.', idle: 'Guía cerrada: nadie la tocaba.', agent: 'Guía terminada.', shutdown: 'Guía terminada.' }[session.ended_by] || 'Guía terminada.';
      body.append(el('div', 'cp-say', session.message || why));
    }
    if (ui.asking && session.status !== 'stopped') body.append(askBox(session));
    else ask = null;
    if (ui.listOpen && session.steps.length) {
      const list = el('ol', 'cp-steps');
      session.steps.forEach((s, i) => {
        const li = el('li', i < session.index ? 'cp-done' : i === session.index && session.status === 'active' ? 'cp-now' : '', `${i + 1}. ${s.say}`);
        list.append(li);
      });
      body.append(list);
    }
    if (session.status !== 'stopped') {
      const next = { dock: 'bottom', bottom: 'top', top: 'dock' }[ui.place];
      acts.append(button('⇅', `Mover la barra: ahora ${{ dock: 'debajo de la imagen', bottom: 'dentro, abajo', top: 'dentro, arriba' }[ui.place]}`, () => {
        ui.place = next;
        keep.set('bar', next);
        render();
      }));
      acts.append(button('✕', 'Parar la guía', () => act('stop')));
    }
    hud.append(who, body, acts);
    if (ask && (ask.fresh || typing)) {
      ask.fresh = false;
      ask.input.focus();
    }
    // Inside the picture the bar keeps away from the marks.
    const low = step && step.marks.some((m) => m.state !== 'searching' && ((m.y != null && m.y > 0.74) || (m.box && m.box[1] + m.box[3] > 0.76)));
    const high = step && step.marks.some((m) => m.state !== 'searching' && ((m.y != null && m.y < 0.26) || (m.box && m.box[1] < 0.24)));
    hud.classList.toggle('cp-top', within && (low && !high ? true : high && !low ? false : ui.place === 'top'));
  }

  // ---------------------------------------------------------------- the conversation beside the picture

  const talkName = el('span', 'cp-talk-name');
  const talkGoal = el('div', 'cp-talk-goal');
  const voiceBtn = el('button', 'btn small');
  voiceBtn.type = 'button';
  const lines = el('ol', 'cp-lines');
  const reply = el('form', 'cp-reply');
  const replyInput = el('input');
  replyInput.type = 'text';
  replyInput.maxLength = 300;
  replyInput.autocomplete = 'off';
  replyInput.placeholder = 'Escríbele al copilot…';
  replyInput.setAttribute('aria-label', 'Escríbele al copilot');
  const replySend = el('button', 'btn small', 'Enviar');
  reply.append(replyInput, replySend);
  const talkHead = el('header', 'cp-talk-head');
  talkHead.append(talkName, voiceBtn);
  talk.append(talkHead, talkGoal, lines, reply);
  reply.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = replyInput.value.trim();
    if (!text) return;
    replyInput.value = '';
    act('say', text);
  });
  voiceBtn.addEventListener('click', () => setVoice(!ui.voice));

  function setVoice(on) {
    ui.voice = on && 'speechSynthesis' in window;
    keep.set('voice', ui.voice ? '1' : '0');
    if (!ui.voice && 'speechSynthesis' in window) speechSynthesis.cancel();
    if (on && !ui.voice) note('Este navegador no puede leer en voz alta.', 'err');
    render();
  }

  function speak(text, fresh) {
    if (!ui.voice || !('speechSynthesis' in window) || !text) return;
    if (fresh) speechSynthesis.cancel();                // a new step: what was being read is old news
    const words = new SpeechSynthesisUtterance(text);
    const voice = speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith('es'));
    if (voice) words.voice = voice;
    words.lang = voice ? voice.lang : 'es-ES';
    words.rate = 1.05;
    speechSynthesis.speak(words);
  }

  function renderTalk(session) {
    talk.hidden = !session;
    if (!session) { ui.heard = null; ui.logKey = ''; return; }
    talkName.textContent = '';
    const ear = el('i', 'cp-ear');
    ear.dataset.on = session.listening ? '1' : '0';
    talkName.append(ear, `Copilot${session.agent ? ` · ${session.agent}` : ''}`);
    talkName.title = session.listening ? 'El copilot está atento.' : 'El copilot no está escuchando ahora: si le escribes, lo leerá cuando vuelva.';
    talkGoal.textContent = session.goal || '';
    talkGoal.hidden = !session.goal;
    voiceBtn.textContent = ui.voice ? '🔊 Voz' : '🔇 Voz';
    voiceBtn.title = ui.voice ? 'Deja de leer las instrucciones en voz alta' : 'Leer las instrucciones en voz alta';
    voiceBtn.classList.toggle('on', ui.voice);
    reply.hidden = session.status === 'stopped';
    const log = session.log || [];
    const key = `${session.id}|${session.status}|${session.index}|` + log.map((e) => `${e.id}${e.done ? 'd' : ''}`).join(',');
    if (key !== ui.logKey) {
      ui.logKey = key;
      lines.textContent = '';
      for (const entry of log) {
        const li = el('li', `cp-line cp-${entry.who}`);
        if (entry.who === 'step') {
          const now = session.status === 'active' && entry.n === session.index + 1;
          li.classList.toggle('cp-now', now);
          li.classList.toggle('cp-done', !!entry.done && !now);
          li.append(el('b', '', entry.done && !now ? '✓' : String(entry.n)), el('span', '', entry.text));
          if (entry.hint) li.append(el('small', '', entry.hint));
        } else {
          li.textContent = entry.text;
        }
        lines.append(li);
      }
      lines.scrollTop = lines.scrollHeight;
    }
    // Read aloud what is new since this page has been watching (never the whole history on opening it).
    const newest = log.length ? log[log.length - 1].id : 0;
    if (ui.heard === null || ui.heard.session !== session.id) ui.heard = { session: session.id, id: newest };
    for (const entry of log) {
      if (entry.id > ui.heard.id && entry.who !== 'you') speak(entry.who === 'step' && entry.hint ? `${entry.text}. ${entry.hint}` : entry.text, entry.who === 'step');
    }
    ui.heard.id = Math.max(ui.heard.id, newest);
  }

  function render() {
    const state = ui.state;
    const session = state && state.session;
    opener.hidden = !state;
    opener.dataset.on = session && session.status !== 'stopped' ? '1' : '0';
    opener.title = session && session.status !== 'stopped'
      ? `Copilot en marcha${session.agent ? ` (${session.agent})` : ''}. Pulsa para copiar su enlace y ver las opciones.`
      : 'Copilot: pulsa para copiar el enlace que le pasas al chat del agente (o arrástralo). Con él te guía sobre el directo.';
    renderHud(session);
    renderTalk(session);
    const box = layout();                 // after the bar: showing it under the picture changes the picture's size
    renderMarks(session, box);
    if (ui.panel) fillPanel();
  }

  // ---------------------------------------------------------------- the top-bar button and its panel

  async function copy(text, done) {
    try { await navigator.clipboard.writeText(text); }
    catch {
      const area = el('textarea');
      area.value = text;
      Object.assign(area.style, { position: 'fixed', opacity: '0' });
      document.body.appendChild(area);
      area.select();
      try { document.execCommand('copy'); } finally { area.remove(); }
    }
    note(done, 'ok');
  }

  function dragAddress(e) {
    e.dataTransfer.effectAllowed = 'copyLink';
    e.dataTransfer.setData('text/uri-list', ADDRESS);
    e.dataTransfer.setData('text/plain', ADDRESS);
  }

  const DEMO = { agent: 'Demostración', goal: 'Así se ve una guía', space: [1000, 1000],
    message: 'Esto es una demostración. Aquí al lado te hablo y me puedes contestar; debajo de la imagen va lo que toca hacer en cada paso.', steps: [
    { say: 'Así te marca dónde pulsar', click: [300, 320], label: 'Aquí', until: 'manual' },
    { say: 'Así te dice qué escribir y dónde', type: 'texto de ejemplo', at: [520, 470, 260, 44], until: 'manual' },
    { say: 'Las teclas salen aquí, y un recuadro señala lo que hay que mirar', keys: 'Ctrl+S', box: [600, 140, 300, 220], until: 'manual' },
    { say: 'Y una flecha cuando toca arrastrar', arrow: [[220, 700], [720, 620]], until: 'manual' }] };

  function fillPanel() {
    const panel = ui.panel, state = ui.state || {}, session = state.session;
    panel.textContent = '';
    panel.append(el('h2', '', 'Copilot'));
    panel.append(el('p', '', 'Un agente te guía sobre el directo: marca dónde pulsar, qué escribir y qué teclas, y pasa al siguiente paso solo, mirando la pantalla.'));
    const row = el('div', 'cp-url');
    const link = el('a', '', ADDRESS);
    link.href = ADDRESS;
    link.target = '_blank';
    link.rel = 'noopener';
    link.draggable = true;
    link.title = 'Arrástralo al chat, o ábrelo para ver lo que lee el agente';
    link.addEventListener('dragstart', dragAddress);
    row.append(link);
    const copies = el('div', 'cp-row');
    const b1 = el('button', 'btn small', 'Copiar enlace');
    b1.addEventListener('click', () => copy(ADDRESS, 'Enlace copiado. Pégalo en el chat del agente.'));
    const b2 = el('button', 'btn small', 'Copiar frase para el chat');
    b2.title = PHRASE;
    b2.addEventListener('click', () => copy(PHRASE, 'Frase copiada. Pégala en el chat y añade qué quieres hacer.'));
    copies.append(b1, b2);
    panel.append(row, copies);
    panel.append(el('p', '', 'Pásale el enlace a cualquier chat que pueda abrirlo en este equipo: ahí lee cómo se usa y empieza. Los agentes con el conector de Baird tienen lo mismo en las herramientas copilot_*.'));
    const now = el('div', 'cp-row');
    const active = session && session.status !== 'stopped';
    now.append(el('span', 'cp-now', active
      ? `${session.agent || 'Un agente'}: ${session.status === 'done' ? 'plan terminado, esperando' : `paso ${session.index + 1} de ${session.count}`}`
      : 'Sin guía en marcha.'));
    if (active) {
      const stop = el('button', 'btn small danger', 'Parar');
      stop.addEventListener('click', () => act('stop'));
      now.append(stop);
    } else if (state.enabled) {
      const demo = el('button', 'btn small', 'Ver una demostración');
      demo.addEventListener('click', async () => {
        closePanel();
        try { await call('/copilot/show', DEMO); } catch (err) { note(err.message, 'err'); }
      });
      now.append(demo);
    }
    panel.append(now);
    const allow = el('label');
    const check = el('input');
    check.type = 'checkbox';
    check.checked = !!state.enabled;
    check.addEventListener('change', async () => {
      try { await call('/api/settings', { copilot: { enabled: check.checked } }, 'PUT'); await load(); }
      catch (err) { note(err.message, 'err'); }
    });
    allow.append(check, 'Permitir que un agente dibuje sobre el directo');
    panel.append(allow);
    const aloud = el('label');
    const voice = el('input');
    voice.type = 'checkbox';
    voice.checked = ui.voice;
    voice.addEventListener('change', () => setVoice(voice.checked));
    aloud.append(voice, 'Leer las instrucciones en voz alta');
    panel.append(aloud);
    if (!state.reads_text) panel.append(el('p', '', 'En este equipo no se puede leer el texto de la pantalla: las marcas van solo por coordenadas.'));
  }

  function closePanel() {
    if (!ui.panel) return;
    ui.panel.remove();
    ui.panel = null;
    document.removeEventListener('pointerdown', outside, true);
  }

  function outside(e) {
    if (ui.panel && !ui.panel.contains(e.target) && !opener.contains(e.target)) closePanel();
  }

  function openPanel() {
    const panel = ui.panel = el('div', 'cp-panel');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Copilot');
    fillPanel();
    document.body.appendChild(panel);
    const r = opener.getBoundingClientRect();
    panel.style.top = `${r.bottom + 8}px`;
    panel.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - panel.offsetWidth - 8))}px`;
    document.addEventListener('pointerdown', outside, true);
  }

  const shown = document.getElementById('copilot-addr');
  if (shown) shown.textContent = `${location.host}/copilot`;
  opener.addEventListener('click', () => {
    if (ui.panel) { closePanel(); return; }
    copy(ADDRESS, 'Enlace del copilot copiado. Pégalo en el chat del agente.');
    openPanel();
  });
  opener.addEventListener('dragstart', dragAddress);

  // ---------------------------------------------------------------- keys, sizes, start

  document.addEventListener('keydown', (e) => {
    const session = ui.state && ui.state.session;
    if (e.key === 'Escape' && ui.panel) { closePanel(); return; }
    if (!session || session.status === 'stopped' || e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target && e.target.tagName) || '';
    if (/^(INPUT|SELECT|TEXTAREA|BUTTON|A)$/.test(tag) || (e.target && e.target.isContentEditable)) return;
    if (document.querySelector('dialog[open]') || ui.panel) return;
    if (typeof zonePicking !== 'undefined' && zonePicking) return;
    const key = e.key;
    if ((key === ' ' || key === 'Enter' || key === 'ArrowRight') && session.status === 'active') { act('next'); e.preventDefault(); }
    else if (key === 'ArrowLeft' || key === 'Backspace') { act('back'); e.preventDefault(); }
    else if (key.toLowerCase() === 'h') { askHelp(); e.preventDefault(); }
  });

  if (window.ResizeObserver) new ResizeObserver(() => render()).observe(stage);
  const video = document.getElementById('video');
  if (video) for (const type of ['resize', 'loadedmetadata']) video.addEventListener(type, () => render());
  document.addEventListener('fullscreenchange', () => render());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { load(); listen(); } });

  (async function start() {
    if (await load()) listen();
    else setTimeout(start, 15000);      // a server without the copilot (or not up yet): look again later
  })();
})();
