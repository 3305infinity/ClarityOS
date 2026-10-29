'use strict';
/* Moonwalk website — talks only to the local hub (same origin). All server text is rendered with
   textContent (never innerHTML). The token arrives in the URL #fragment, is moved to sessionStorage
   and removed from the address bar. API keys are write-only: the hub never sends them back. */
const $ = (s) => document.querySelector(s);
const S = { ws: null, state: null, busy: false, paused: false, rec: null, q: [], playing: null, ctx: null, retry: 1000, tools: {} };

// ── token ──
(() => {
  const m = location.hash.match(/token=([\w-]+)/);
  if (m) { sessionStorage.setItem('mw_token', m[1]); history.replaceState(null, '', location.pathname); }
})();
const token = () => sessionStorage.getItem('mw_token') || '';

async function api(path, method = 'GET', body) {
  const r = await fetch(path, { method, headers: { Authorization: 'Bearer ' + token(), 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) { banner('Not signed in — open the link printed by the hub (it contains your token), or run start-hub.ps1.'); throw new Error('401'); }
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  return r;
}
const json = async (...a) => (await api(...a)).json();

// ── tiny DOM helpers ──
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) { if (k === 'class') n.className = v; else if (k.startsWith('on')) n.addEventListener(k.slice(2), v); else if (v !== undefined) n[k] = v; }
  for (const k of kids.flat()) n.append(k instanceof Node ? k : document.createTextNode(String(k ?? '')));
  return n;
}
function banner(t) { const b = $('#banner'); b.hidden = !t; b.textContent = t || ''; }
/* ══════════════════════════════════════════════════════════════
   Markdown → DOM  (assistant messages only)
   ══════════════════════════════════════════════════════════════
   Builds real elements with createElement + textContent. Nothing is ever
   parsed as HTML, so assistant output cannot inject markup — no innerHTML,
   no sanitizer, and no new dependency (the page's CSP is script-src 'self',
   so a CDN parser could not load anyway).

   Tolerant of partial input: an unterminated fence or a half-written table
   still renders as something sensible rather than throwing. */

const MD_SAFE_SCHEME = /^(https?:|mailto:)/i;

function mdInline(parent, text) {
  // One pass, longest-token-first so ** wins over *.
  const RE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(__[^_\n]+__)|(\*[^*\n]+\*)|(_[^_\n]+_)|(\[[^\]\n]*\]\([^)\s]+\))|(https?:\/\/[^\s<>()]+)/g;
  let last = 0, m;
  while ((m = RE.exec(text)) !== null) {
    if (m.index > last) parent.append(document.createTextNode(text.slice(last, m.index)));
    const t = m[0];
    if (m[1]) parent.append(el('code', {}, t.slice(1, -1)));
    else if (m[2]) parent.append(el('strong', {}, t.slice(2, -2)));
    else if (m[3]) parent.append(el('strong', {}, t.slice(2, -2)));
    else if (m[4]) parent.append(el('em', {}, t.slice(1, -1)));
    else if (m[5]) parent.append(el('em', {}, t.slice(1, -1)));
    else if (m[6]) {
      const cut = t.indexOf(']');
      const label = t.slice(1, cut), href = t.slice(cut + 2, -1);
      if (MD_SAFE_SCHEME.test(href)) {
        parent.append(el('a', { href, target: '_blank', rel: 'noopener noreferrer' }, label || href));
      } else {
        parent.append(document.createTextNode(t));   // javascript:/data: → plain text
      }
    } else if (m[7]) {
      parent.append(el('a', { href: t, target: '_blank', rel: 'noopener noreferrer' }, t));
    }
    last = m.index + t.length;
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
}

function mdTableRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}
const MD_IS_DIVIDER = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

function renderMarkdownInto(root, src) {
  const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
  let i = 0;

  const paraBuf = [];
  const flushPara = () => {
    if (!paraBuf.length) return;
    const p = el('p', {});
    mdInline(p, paraBuf.join('\n'));
    root.append(p);
    paraBuf.length = 0;
  };

  while (i < lines.length) {
    const line = lines[i];

    // fenced code — unterminated fences run to the end rather than throwing
    const fence = line.match(/^\s*```(\w+)?\s*$/);
    if (fence) {
      flushPara();
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;                                   // consume the closing fence if present
      const pre = el('pre', { class: 'md-pre' });
      pre.append(el('code', {}, body.join('\n')));
      root.append(pre);
      continue;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (heading) {
      flushPara();
      const h = el('h' + heading[1].length, { class: 'md-h' });
      mdInline(h, heading[2].trim());
      root.append(h);
      i++;
      continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      flushPara(); root.append(el('hr', { class: 'md-hr' })); i++; continue;
    }

    // table: header row followed by a divider row
    if (line.includes('|') && i + 1 < lines.length && MD_IS_DIVIDER(lines[i + 1])) {
      flushPara();
      const head = mdTableRow(line);
      i += 2;
      const table = el('table', { class: 'md-table' });
      const thead = el('thead', {}), hr = el('tr', {});
      head.forEach((c) => { const th = el('th', {}); mdInline(th, c); hr.append(th); });
      thead.append(hr); table.append(thead);
      const tbody = el('tbody', {});
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        const tr = el('tr', {});
        mdTableRow(lines[i]).forEach((c) => { const td = el('td', {}); mdInline(td, c); tr.append(td); });
        tbody.append(tr); i++;
      }
      table.append(tbody);
      root.append(el('div', { class: 'md-table-wrap' }, table));
      continue;
    }

    // lists (one level; nested bullets are flattened rather than dropped)
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const number = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || number) {
      flushPara();
      const ordered = !!number;
      const list = el(ordered ? 'ol' : 'ul', { class: 'md-list' });
      while (i < lines.length) {
        const b = lines[i].match(/^\s*[-*+]\s+(.*)$/);
        const n = lines[i].match(/^\s*\d+[.)]\s+(.*)$/);
        if (!(ordered ? n : b)) break;
        const li = el('li', {});
        mdInline(li, (ordered ? n[1] : b[1]).trim());
        list.append(li);
        i++;
      }
      root.append(list);
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flushPara();
      const bq = el('blockquote', { class: 'md-quote' });
      const parts = [quote[1]];
      i++;
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { parts.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
      mdInline(bq, parts.join('\n'));
      root.append(bq);
      continue;
    }

    if (!line.trim()) { flushPara(); i++; continue; }

    paraBuf.push(line);
    i++;
  }
  flushPara();
  return root;
}

function msg(text, cls = '', markdown = false) {
  hideWelcome();
  const m = el('div', { class: 'msg ' + cls + (markdown ? ' md' : '') });
  if (markdown) renderMarkdownInto(m, text); else m.append(document.createTextNode(String(text ?? '')));
  $('#log').append(m); m.scrollIntoView({ block: 'end' }); return m;
}
function step(text, cls = 'run', sub = '') {
  const li = el('li', { class: cls }, text, sub ? el('small', {}, sub) : ''); $('#steps').append(li); li.scrollIntoView({ block: 'end' });
  const empty = $('#act-empty'); if (empty) empty.hidden = true;
  bumpActivity();
  return li;
}

// ── welcome state ──
function hideWelcome() {
  const w = $('#welcome'); if (w && !w.hidden) w.hidden = true;
  $('#chat').classList.remove('landing');
  const fx = $('#herofx'); if (fx && !fx.hidden) {
    fx.hidden = true;
    if (S.fxStop) { S.fxStop(); S.fxStop = null; }
    // Release the GPU context too — browsers cap live WebGL contexts.
    if (S.metalStop) { S.metalStop(); S.metalStop = null; }
  }
}
$('#chat').classList.add('landing');     // until the first message arrives

/* ══════════════════════════════════════════════════════════════
   Perspective tunnel — the landing visual behind the welcome state.
   Golden-angle card placement flying far→near in Z, spring-smoothed,
   driven by the wheel while the welcome state is up and by a slow
   idle drift otherwise. Stops entirely once a conversation starts.
   ══════════════════════════════════════════════════════════════ */
(function tunnel() {
  const plane = $('#fxPlane'), fx = $('#herofx'), chat = $('#chat'), welcome = $('#welcome');
  if (!plane || !fx) return;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // The pipeline a request actually travels through inside ClarityOS.
  const CARDS = [
    { step: '01', title: 'Voice Input', hue: 258 },
    { step: '02', title: 'Transcription', hue: 244 },
    { step: '03', title: 'Screen Context', hue: 232 },
    { step: '04', title: 'Task Planner', hue: 220 },
    { step: '05', title: 'Tool Registry', hue: 206 },
    { step: '06', title: 'Approval Gate', hue: 38 },
    { step: '07', title: 'Desktop Action', hue: 199 },
    { step: '08', title: 'Verification', hue: 165 },
    { step: '09', title: 'Spoken Reply', hue: 270 },
  ];
  const STAGGER = 0.55;
  let stage = { w: chat.clientWidth, h: chat.clientHeight, mobile: chat.clientWidth < 768 };

  const nodes = CARDS.map((c, i) => {
    const golden = i * 137.5 * (Math.PI / 180);
    const n = el('div', { class: 'fxcard' });
    // Hue only — the light/dark rendering of the card is decided in CSS.
    n.style.setProperty('--hue', String(c.hue));
    const art = el('div', { class: 'fxcard-art' });
    const inner = el('div', { class: 'fxcard-inner' }, art,
      el('div', { class: 'fxcard-label' },
        el('span', { class: 'fxcard-step' }, c.step),
        el('span', { class: 'fxcard-title' }, c.title)));
    n.append(inner); plane.append(n);
    return {
      // Pushed further out than the reference so the corridor frames the
      // dashboard content instead of crossing straight through it.
      el: n, cos: Math.cos(golden), sin: Math.sin(golden),
      rx: 0.40 + (i % 3) * 0.07, ry: 0.36 + (i % 2) * 0.08,
      rot: (i % 2 === 0 ? 1 : -1) * (3 + (i % 5) * 2),
      start: (i / CARDS.length) * (1 - STAGGER),
    };
  });

  function sizeCards() {
    stage = { w: chat.clientWidth, h: chat.clientHeight, mobile: chat.clientWidth < 768 };
    const cw = stage.mobile
      ? Math.max(110, Math.min(150, stage.w * 0.34))
      : Math.max(150, Math.min(220, stage.w * 0.16));
    nodes.forEach((n) => { n.el.style.width = cw + 'px'; });
  }
  sizeCards();
  const onResize = () => sizeCards();
  window.addEventListener('resize', onResize, { passive: true });

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  function mapRange(v, inp, out) {
    if (v <= inp[0]) return out[0];
    for (let i = 1; i < inp.length; i++) {
      if (v <= inp[i]) { const t = (v - inp[i - 1]) / (inp[i] - inp[i - 1] || 1); return out[i - 1] + t * (out[i] - out[i - 1]); }
    }
    return out[out.length - 1];
  }
  function Spring(k, d, m) { this.k = k; this.d = d; this.m = m || 1; this.value = 0; this.target = 0; this.vel = 0; }
  Spring.prototype.step = function (dt) {
    const a = (-this.k * (this.value - this.target) - this.d * this.vel) / this.m;
    this.vel += a * dt; this.value += this.vel * dt;
  };

  const prog = new Spring(80, 30, 1), px = new Spring(60, 25, 0.5), py = new Spring(60, 25, 0.5);

  // Re-read on every theme change so the toggle takes effect immediately.
  let FX_ALPHA = 0.42;
  function syncAlpha() {
    FX_ALPHA = document.documentElement.dataset.theme === 'light' ? 0.38 : 0.42;
  }
  syncAlpha();
  new MutationObserver(syncAlpha).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });

  if (reduced) { prog.target = prog.value = 0.45; }

  if (!reduced) {
    // The corridor is endless, so the wheel only steers it — there is nothing
    // else to scroll while the welcome state is up, and once a conversation
    // starts this handler bails out and the message list scrolls normally.
    $('#scroll').addEventListener('wheel', (e) => {
      if (fx.hidden || (welcome && welcome.hidden)) return;
      e.preventDefault();
      prog.target += e.deltaY * 0.0008;
    }, { passive: false });

    if (matchMedia('(pointer: fine)').matches) {
      chat.addEventListener('pointermove', (e) => {
        const r = chat.getBoundingClientRect();
        px.target = ((e.clientX - r.left) / r.width - 0.5) * 2;
        py.target = ((e.clientY - r.top) / r.height - 0.5) * 2;
      }, { passive: true });
    }
  }

  let raf = 0, last = performance.now();
  function frame(now) {
    const dt = Math.min((now - last) / 1000, 0.05); last = now;
    if (!reduced) prog.target += dt * 0.05;          // gentle continuous drift

    prog.step(dt); px.step(dt); py.step(dt);
    const p = prog.value;
    const farZ = stage.mobile ? -1200 : -1700, nearZ = stage.mobile ? 350 : 500;
    const pan = stage.mobile ? 18 : 40;

    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      // Wrap each card's phase so the corridor never runs out of cards.
      const cycle = (p - n.start) / STAGGER;
      const local = ((cycle % 1) + 1) % 1;
      const z = mapRange(local, [0, 1], [farZ, nearZ]);
      // Scaled down: this sits behind a working dashboard, not a standalone
      // hero. Light mode needs to be fainter still — dark cards on a white
      // page read as dirt long before they read as depth.
      const op = mapRange(local, [0, .18, .82, .98], [0, 1, 1, 0]) * FX_ALPHA;
      const ox = n.cos * (stage.w * n.rx) - px.value * pan;
      const oy = n.sin * (stage.h * n.ry) - py.value * pan;
      n.el.style.transform =
        `translate3d(calc(-50% + ${ox.toFixed(1)}px), calc(-50% + ${oy.toFixed(1)}px), ${z.toFixed(1)}px) rotateZ(${n.rot}deg)`;
      n.el.style.opacity = op.toFixed(3);
      n.el.style.zIndex = String(i + 1);
    }
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);

  S.fxStop = () => { cancelAnimationFrame(raf); window.removeEventListener('resize', onResize); };
})();

// ── activity rail ──
function bumpActivity() {
  const b = $('#act-count'); if (!b) return;
  b.textContent = String($('#steps').childElementCount);
  b.hidden = $('#timeline').hidden === false || !$('#steps').childElementCount;
}
function showPanel(name) {
  document.querySelectorAll('.nav-item').forEach((n) => {
    const on = n.dataset.panel === name;
    n.classList.toggle('on', on);
    if (on) n.setAttribute('aria-current', 'page'); else n.removeAttribute('aria-current');
  });
  $('#timeline').hidden = name !== 'activity';
  bumpActivity();
}
document.querySelectorAll('.nav-item').forEach((n) => (n.onclick = () => showPanel(n.dataset.panel)));
$('#act-close').onclick = () => showPanel('chat');

// Suggested actions send real commands through the normal command path.
document.querySelectorAll('.sg').forEach((b) => (b.onclick = () => {
  if (S.busy) return;
  $('#in').value = b.dataset.cmd;
  sendText();
}));
// Name on the left, state on the right. Cramming "Chrome ✗" into one string
// read like an error code; the label never changes, only its value does.
function pill(id, label, value, mode) {
  const p = $(id);
  if (!p) return;
  p.querySelector('.pill-k').textContent = label;
  p.querySelector('.pill-v').textContent = value;
  p.className = 'pill ' + mode;
  p.title = `${label}: ${value}`;
}

// ── extension pairing approval ──
// The extension cannot pair itself: it asks, and this prompt is the only thing
// that releases the pairing code. Deny-by-default — closing the dialog denies.
function askPairing(m) {
  const dlg = $('#d-pair');
  if (!dlg || dlg.open) return;
  dlg.dataset.id = m.request_id;
  $('#pr-why').textContent =
    `“${m.extension_name || 'A Chrome extension'}” is asking to connect to ClarityOS` +
    (m.origin ? ` (${m.origin})` : '') + '.';

  let settled = false;
  const answer = (approve) => {
    if (settled) return;
    settled = true;
    send({ type: 'bridge_pair_response', request_id: m.request_id, approve });
    if (dlg.open) dlg.close();
  };
  $('#pr-yes').onclick = () => answer(true);
  $('#pr-no').onclick = () => answer(false);
  dlg.onclose = () => answer(false);
  // Mirror the backend's own expiry so a stale prompt cannot linger.
  setTimeout(() => { if (!settled && dlg.open) dlg.close(); }, Math.max(5, Number(m.timeout) || 120) * 1000);
  dlg.showModal();
}

// ── websocket ──
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`); S.ws = ws;
  ws.onopen = () => ws.send(JSON.stringify({ type: 'auth', token: token() }));
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = (e) => {
    pill('#p-hub', 'Hub', 'Offline', 'off'); S.busy = false; render();
    if (e.code === 4401) { banner('Wrong or missing token. Open the link the hub printed (it contains #token=…).'); return; }
    setTimeout(connect, S.retry); S.retry = Math.min(S.retry * 2, 15000);
  };
}
const send = (o) => S.ws && S.ws.readyState === 1 && S.ws.send(JSON.stringify(o));

function conns(c) {
  const voiceReady = c.voice.stt_ready && c.voice.tts_ready;
  pill('#p-hub', 'Hub', 'Connected', 'on');
  pill('#p-chrome', 'Chrome', c.browser.connected ? 'Connected' : 'Not connected', c.browser.connected ? 'on' : 'off');
  pill('#p-helper', 'Windows', c.helper.connected ? 'Connected' : 'Optional', c.helper.connected ? 'on' : 'warn');
  pill('#p-voice', 'Voice', voiceReady ? `${c.voice.stt_provider} · ${c.voice.tts_provider}` : 'Not ready', voiceReady ? 'on' : 'warn');
  S.busy = c.busy; S.paused = c.paused;
  banner(c.brain_ready ? '' : 'Gemini API key missing — Settings → API keys.');
  render();
}
function render() {
  $('#b-stop').disabled = !S.busy; $('#b-pause').disabled = !S.busy; $('#b-pause').textContent = S.paused ? 'Resume' : 'Pause';
  $('#b-send').disabled = S.busy;
}

function handle(m) {
  switch (m.type) {
    case 'hello': S.state = m.state; S.retry = 1000; conns(m.state.connections); applyTheme(m.state.settings.theme); break;
    case 'connections': conns(m.connections); break;
    case 'task_started': $('#steps').replaceChildren(); S.tools = {}; S.busy = true; hideWelcome(); render(); step('Task started', 'ok', m.source); break;
    case 'step': if (m.text) step(m.text, 'run', m.tool || ''); break;
    case 'tool_start': S.tools[m.tool + m.ts] = step(m.tool, 'run', m.reason || ''); S.lastTool = S.tools[m.tool + m.ts]; break;
    case 'tool_result': { const li = S.lastTool; if (li) { li.className = m.ok ? 'ok' : 'fail'; li.append(el('small', {}, `${m.ms} ms · ${(m.preview || '').slice(0, 160)}`)); } else step(m.tool, m.ok ? 'ok' : 'fail', m.preview); break; }
    case 'tool_blocked': step(`${m.tool} blocked`, 'fail', m.reason); break;
    case 'captcha_detected': msg('A CAPTCHA / verification is on the page. Please complete it yourself — ClarityOS will not bypass it.', 'sys'); break;
    case 'response': if (m.text || m.modal) showResponse(m); break;
    case 'transcribing': $('#in').placeholder = 'Samajh raha hoon… / Transcribing…'; break;
    case 'transcript': $('#in').placeholder = 'Bolo ya likho… / Speak or type'; msg(m.text, 'me'); break;
    case 'transcript_error': $('#in').placeholder = 'Bolo ya likho… / Speak or type'; msg(m.message + (m.hint ? ' ' + m.hint : ''), 'err'); $('#in').focus(); break;
    case 'error': msg(m.message + (m.hint ? '\n→ ' + m.hint : ''), 'err'); break;
    case 'voice_warning': msg(m.message, 'sys'); break;
    case 'approval_request': askApproval(m); break;
    case 'bridge_pair_request': askPairing(m); break;
    case 'approval_resolved': if ($('#d-approve').open && $('#d-approve').dataset.id === m.approval_id) $('#d-approve').close(); break;
    case 'tts_chunk': enqueueAudio(m); break;
    case 'tts_stop': stopAudio(); break;
    case 'speaking': $('#hint').textContent = m.state ? '🔊 Speaking… press Esc or the mic to interrupt' : 'Hold Space = talk · Enter = send · Esc = stop'; break;
    case 'paused': S.paused = true; render(); step('Paused — press Resume', 'run'); break;
    case 'resumed': S.paused = false; render(); break;
    case 'task_finished': S.busy = false; render(); step(`Finished: ${m.status}`, m.status === 'done' ? 'ok' : 'fail'); break;
    case 'cancelled': stopAudio(); break;
    case 'workflow_step': msg(`Workflow “${m.workflow}” ${m.index}/${m.total}: ${m.text}`, 'sys'); break;
    case 'chat_reset': $('#log').replaceChildren(); $('#welcome').hidden = false; break;
  }
}

function showResponse(m) {
  // Assistant text is the only Markdown-rendered path. User, system and error
  // messages stay plain so they read exactly as sent.
  const box = msg(m.text || '', '', true);
  const modal = m.modal && typeof m.modal === 'object' ? m.modal : null;
  if (modal && modal.modal === 'plan' && Array.isArray(modal.steps)) {       // the agent's plan preview
    const ol = el('ol', { class: 'plan' }); modal.steps.forEach((s) => ol.append(el('li', {}, s.label || '', s.detail ? ' — ' + s.detail : '')));
    box.append(ol, el('div', { class: 'btns' }, el('button', { onclick: () => { send({ type: 'plan_decision', approve: false }); box.querySelector('.btns').remove(); } }, 'Cancel'),
      el('button', { onclick: () => { send({ type: 'plan_decision', approve: true }); box.querySelector('.btns').remove(); } }, 'Proceed')));
  }
  if (m.await_input) $('#in').focus();
}

// ── approvals ──
function askApproval(m) {
  const d = $('#d-approve'); d.dataset.id = m.approval_id;
  $('#ap-why').textContent = `${m.capability}: ${m.reason}`; $('#ap-sum').textContent = m.summary;
  const answer = (ok) => { send({ type: 'approval_response', approval_id: m.approval_id, approve: ok }); d.close(); };
  $('#ap-yes').onclick = () => answer(true); $('#ap-no').onclick = () => answer(false);
  d.oncancel = (e) => { e.preventDefault(); answer(false); };
  if (!d.open) d.showModal(); $('#ap-no').focus();
}

// ── commands ──
function sendText() {
  const i = $('#in'), t = i.value.trim(); if (!t || S.busy) return;
  stopAudio(); msg(t, 'me'); send({ type: 'command', text: t }); i.value = '';
}
$('#b-send').onclick = sendText;
$('#in').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) sendText(); });
function stopAll() { send({ type: 'cancel' }); stopAudio(); if (S.rec) stopRec(false); }
$('#b-stop').onclick = stopAll;
$('#b-pause').onclick = () => send({ type: S.paused ? 'resume' : 'pause' });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !document.querySelector('dialog[open]')) stopAll();
  if (e.code === 'Space' && !e.repeat && document.activeElement !== $('#in') && !document.querySelector('dialog[open]') && !S.rec) { e.preventDefault(); startRec(true); }
});
document.addEventListener('keyup', (e) => { if (e.code === 'Space' && S.rec && S.rec.hold) stopRec(true); });
$('#b-mic').onclick = () => (S.rec ? stopRec(true) : startRec(false));

// ── microphone → 16 kHz mono WAV → hub (STT happens server-side: Gnani / Sarvam / Google) ──
async function startRec(hold) {
  if (S.busy) return;
  stopAudio();
  try {
    const ctx = new AudioContext({ sampleRate: 16000 });
    await ctx.audioWorklet.addModule('/static/worklet.js');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    const src = ctx.createMediaStreamSource(stream), node = new AudioWorkletNode(ctx, 'pcm'), an = ctx.createAnalyser();
    an.fftSize = 512; src.connect(an); src.connect(node);
    const r = S.rec = { ctx, stream, an, chunks: [], hold, voiced: false, quiet: 0, t0: performance.now() };
    node.port.onmessage = (e) => {
      const c = e.data; r.chunks.push(c);
      let s = 0; for (let i = 0; i < c.length; i++) s += c[i] * c[i];
      const rms = Math.sqrt(s / c.length), ms = (c.length / 16000) * 1000;
      if (rms > 0.02) { r.voiced = true; r.quiet = 0; } else r.quiet += ms;
      if (!r.hold && r.voiced && r.quiet > 1300) stopRec(true);                  // silence → end of utterance (click mode)
      if (performance.now() - r.t0 > 29000) stopRec(true);
    };
    $('#b-mic').classList.add('rec'); $('#hint').textContent = '🎤 Listening…'; drawWave();
  } catch (err) {
    S.rec = null; msg('Microphone unavailable (' + err.name + '). You can type your command instead.', 'err'); $('#in').focus();
  }
}
function stopRec(sendIt) {
  const r = S.rec; if (!r) return; S.rec = null;
  r.stream.getTracks().forEach((t) => t.stop()); r.ctx.close();
  $('#b-mic').classList.remove('rec'); $('#hint').textContent = 'Hold Space = talk · Enter = send · Esc = stop';
  if (!sendIt || !r.chunks.length) return;
  send({ type: 'voice_utterance', audio: wavB64(r.chunks) });
}
function wavB64(chunks) {
  const n = chunks.reduce((a, c) => a + c.length, 0), buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const w = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVEfmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, n * 2, true);
  let o = 44; for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) v.setInt16(o, Math.max(-1, Math.min(1, c[i])) * 0x7fff, true);
  let bin = ''; const u = new Uint8Array(buf); for (let i = 0; i < u.length; i += 0x8000) bin += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(bin);
}
function drawWave() {
  const cv = $('#wave'), g = cv.getContext('2d'), col = getComputedStyle(cv).color;
  const loop = () => {
    g.clearRect(0, 0, cv.width, cv.height);
    if (!S.rec) return;
    const d = new Uint8Array(S.rec.an.fftSize); S.rec.an.getByteTimeDomainData(d);
    g.strokeStyle = col; g.lineWidth = 2; g.beginPath();
    d.forEach((x, i) => { const px = (i / d.length) * cv.width, py = (x / 255) * cv.height; i ? g.lineTo(px, py) : g.moveTo(px, py); });
    g.stroke(); requestAnimationFrame(loop);
  }; loop();
}

// ── spoken replies (streamed sentence by sentence) ──
async function enqueueAudio(m) {
  // Drop a re-delivered frame rather than speaking the sentence twice.
  const seq = Number.isFinite(m.seq) ? m.seq : S.q.length;
  S.seen = S.seen || new Set();
  if (S.seen.has(seq)) { console.warn('[TTS] duplicate chunk seq=' + seq + ' ignored'); return; }
  S.seen.add(seq);

  // Keep sentence order even though decode happens asynchronously downstream.
  let i = S.q.length;
  while (i > 0 && (S.q[i - 1].seq ?? 0) > seq) i--;
  S.q.splice(i, 0, m);

  // `starting` closes the window where playNext() awaits decodeAudioData and
  // a second chunk sees S.playing still null, starting an overlapping source.
  if (!S.playing && !S.starting) { S.starting = true; playNext(); }
}
async function playNext() {
  const m = S.q.shift();
  if (!m) { S.playing = null; S.starting = false; S.seen = new Set(); return; }
  try {
    S.ctx = S.ctx || new AudioContext();
    // Chromium starts an AudioContext suspended until a user gesture. Without
    // this the buffer decodes and start() runs, but nothing is ever audible.
    if (S.ctx.state === 'suspended') await S.ctx.resume();
    if (S.ctx.state !== 'running') { audioBlocked(); S.playing = null; return; }

    const bin = Uint8Array.from(atob(m.data), (c) => c.charCodeAt(0));
    const buf = await S.ctx.decodeAudioData(bin.buffer);
    const src = S.ctx.createBufferSource(); src.buffer = buf; src.connect(S.ctx.destination);
    // Only one live source at a time.
    if (S.playing) { try { S.playing.onended = null; S.playing.stop(); } catch {} }
    S.starting = false;
    S.playing = src; src.onended = () => { if (S.playing === src) playNext(); }; src.start();
  } catch (err) {
    // Silence used to be swallowed here, so a decode failure looked identical
    // to working audio. Report it once, then carry on with the next chunk.
    console.error('[TTS] playback failed:', err);
    if (!S.audioWarned) { S.audioWarned = true; msg('Could not play the spoken reply (' + (err && err.name || 'error') + '). The text above is the full response.', 'sys'); }
    S.playing = null; S.starting = false; playNext();
  }
}

// Autoplay policy blocked us: ask for one click, then drain the queue.
function audioBlocked() {
  if (S.audioPrompt) return;
  S.audioPrompt = true;
  banner('Click anywhere to enable spoken replies (the browser blocks audio until you interact).');
  const unlock = async () => {
    document.removeEventListener('pointerdown', unlock);
    document.removeEventListener('keydown', unlock);
    S.audioPrompt = false;
    try { await S.ctx.resume(); } catch {}
    banner('');
    if (!S.playing) playNext();
  };
  document.addEventListener('pointerdown', unlock, { once: true });
  document.addEventListener('keydown', unlock, { once: true });
}
function stopAudio() {
  S.q = []; const p = S.playing; S.playing = null;
  S.starting = false; S.seen = new Set();
  if (p) { p.onended = null; try { p.stop(); } catch {} send({ type: 'stop_speaking' }); }
}

// ── theme ──
function applyTheme(t) { if (t && t !== 'system') document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
$('#b-theme').onclick = async () => {
  const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light'), nxt = cur === 'dark' ? 'light' : 'dark';
  applyTheme(nxt); try { await api('/api/settings', 'PUT', { theme: nxt }); } catch {}
};

// ── settings ──
const dlg = $('#d-settings');
$('#b-settings').onclick = async () => { S.state = await json('/api/state'); tab('voice'); dlg.showModal(); };
$('#st-close').onclick = () => dlg.close();
document.querySelectorAll('#tabs button').forEach((b) => (b.onclick = () => tab(b.dataset.t)));
function field(label, node) { return el('div', { class: 'f' }, el('label', {}, label), node); }
function sel(opts, val, on) { const s = el('select', { onchange: (e) => on(e.target.value) }); opts.forEach((o) => s.append(el('option', { value: o, selected: o === val }, o))); return s; }
async function save(patch) { S.state = await json('/api/settings', 'PUT', patch); }

async function tab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === name));
  const body = $('#tab-body'); body.replaceChildren(); const st = S.state, s = st.settings;
  if (name === 'voice') {
    const o = st.options;
    body.append(
      field('Speech recognition', sel(['google', 'gnani', 'sarvam'], s.stt_provider, (v) => save({ stt_provider: v }))),
      field('Language', sel(o.stt_languages, s.stt_language, (v) => save({ stt_language: v }))),
      field('Voice output', sel(['google', 'gnani', 'sarvam'], s.tts_provider, (v) => save({ tts_provider: v }))),
      field('Speak replies', el('input', { type: 'checkbox', checked: s.tts_enabled, onchange: (e) => save({ tts_enabled: e.target.checked }) })),
      field('Speed ' + s.tts_speed, el('input', { type: 'range', min: 0.5, max: 2, step: 0.05, value: s.tts_speed, onchange: (e) => save({ tts_speed: +e.target.value }).then(() => tab('voice')) })),
      field('Gnani voice', el('input', { type: 'text', value: s.gnani_tts_voice, list: 'gv', onchange: (e) => save({ gnani_tts_voice: e.target.value }) })),
      field('Gnani language', el('input', { type: 'text', value: s.gnani_tts_language, onchange: (e) => save({ gnani_tts_language: e.target.value }) })),
      field('Sarvam speaker', el('input', { type: 'text', value: s.sarvam_tts_speaker, onchange: (e) => save({ sarvam_tts_speaker: e.target.value }) })),
      field('Bolna agent ID', el('input', { type: 'text', value: s.bolna_agent_id, onchange: (e) => save({ bolna_agent_id: e.target.value }) })));
    const dl = el('datalist', { id: 'gv' }); Object.values(o.gnani_voices).flat().forEach((v) => dl.append(el('option', { value: v }))); body.append(dl,
      el('p', { class: 'mut' }, 'Tip: Gnani “hi-en” + voice Poorvi is the Hinglish setup. If the chosen engine fails, Google is used as backup.'));
  } else if (name === 'keys') {
    body.append(el('p', { class: 'mut' }, st.connections.secrets_persistent ? 'Keys are stored in Windows Credential Manager. They are never shown again.' : '⚠ No secure keyring found — keys last only until the hub restarts.'));
    for (const [k, label] of Object.entries(st.secret_labels)) {
      const inp = el('input', { type: 'password', placeholder: st.secrets[k] ? '•••••••• (saved)' : 'paste key', autocomplete: 'off' });
      body.append(el('div', { class: 'row' }, el('span', {}, label), inp,
        el('button', { onclick: async () => { if (!inp.value.trim()) return; try { await api(`/api/secrets/${k}`, 'PUT', { value: inp.value }); inp.value = ''; S.state = await json('/api/state'); tab('keys'); } catch (e) { msg(e.message, 'err'); } } }, 'Save'),
        el('button', { class: 'danger', onclick: async () => { await api(`/api/secrets/${k}`, 'DELETE'); S.state = await json('/api/state'); tab('keys'); } }, 'Remove')));
    }
  } else if (name === 'perm') {
    body.append(el('p', { class: 'mut' }, 'allow = runs (risky actions still ask) · ask = approve every call · deny = never.'));
    for (const cap of st.capabilities) body.append(field(cap, sel(['allow', 'ask', 'deny'], s.permissions[cap], (v) => save({ permissions: { [cap]: v } }))));
    body.append(field('Auto-approve risky actions', el('input', { type: 'checkbox', checked: s.auto_approve_sensitive, onchange: (e) => { if (e.target.checked && !confirm('ClarityOS will no longer ask before sending messages, deleting, paying or calling. Continue?')) { e.target.checked = false; return; } save({ auto_approve_sensitive: e.target.checked }); } })));
  } else if (name === 'conn') {
    const p = await json('/api/pairing'), c = st.connections;
    body.append(el('p', {}, `Chrome extension: ${c.browser.connected ? 'connected' : 'not connected'} · Windows helper: ${c.helper.connected ? 'connected (' + c.helper.host + ')' : 'not connected'}`),
      el('p', { class: 'mut' }, 'Extension → Options: bridge URL and this pairing code. The helper reads its own token automatically on this PC (windows_helper\\start.ps1).'),
      field('Bridge URL', el('input', { type: 'text', readOnly: true, value: p.bridge_url })), field('Pairing code', el('input', { type: 'password', readOnly: true, value: p.bridge_token, onfocus: (e) => (e.target.type = 'text') })),
      el('button', { onclick: async () => { await api('/api/pairing/rotate/bridge', 'POST'); tab('conn'); } }, 'Generate new pairing code'));
  } else if (name === 'hist') {
    const { tasks } = await json('/api/tasks?limit=50');
    body.append(el('div', { class: 'row' }, el('a', { href: '#', onclick: async (e) => { e.preventDefault(); const b = await (await api('/api/export')).blob(); const a = el('a', { href: URL.createObjectURL(b), download: 'clarityos-task-log.json' }); a.click(); } }, 'Export logs (JSON)'),
      el('button', { class: 'danger', onclick: async () => { if (confirm('Delete all task history?')) { await api('/api/tasks', 'DELETE'); tab('hist'); } } }, 'Clear all')));
    tasks.forEach((t) => body.append(el('div', { class: 'row' }, el('span', {}, `${new Date(t.created * 1000).toLocaleString()} · ${t.status} · ${t.text}`),
      el('button', { onclick: () => { $('#in').value = t.text; dlg.close(); $('#in').focus(); } }, 'Reuse'), el('button', { class: 'danger', onclick: async () => { await api('/api/tasks/' + t.id, 'DELETE'); tab('hist'); } }, '✕'))));
    if (!tasks.length) body.append(el('p', { class: 'mut' }, 'No tasks yet.'));
  } else if (name === 'wf') {
    const { workflows } = await json('/api/workflows'), nm = el('input', { type: 'text', placeholder: 'Workflow name' }), ta = el('textarea', { rows: 4, placeholder: 'One step per line, e.g.\nChrome kholo aur GitHub dashboard dikhao\nfailing tests list karo' });
    workflows.forEach((w) => body.append(el('div', { class: 'row' }, el('span', {}, `${w.name} (${w.steps.length} steps)`), el('button', { onclick: () => { send({ type: 'replay_workflow', id: w.id }); dlg.close(); } }, 'Run'), el('button', { class: 'danger', onclick: async () => { await api('/api/workflows/' + w.id, 'DELETE'); tab('wf'); } }, '✕'))));
    body.append(el('h4', {}, 'Save a workflow'), nm, ta, el('button', { onclick: async () => { try { await api('/api/workflows', 'POST', { name: nm.value, steps: ta.value.split('\n') }); tab('wf'); } catch (e) { msg(e.message, 'err'); } } }, 'Save'));
  }
}

/* ══════════════════════════════════════════════════════════════
   Liquid metal ribbons — the hero's signature surface. Theme follows the
   Hub's own data-theme attribute. If WebGL is missing or the context fails,
   nothing is added and the corridor stays exactly as it was.
   ══════════════════════════════════════════════════════════════ */
(function heroMetal() {
  const canvas = $('#fxMetal'), fx = $('#herofx');
  if (!canvas || !fx || typeof createShaderMetal !== 'function') return;

  const metal = createShaderMetal(canvas, {
    metal: 'chrome',
    ribbons: 3,
    speed: 0.26,
    // 0 = bare polished metal. The thin-film sheen tinted the ribbons
    // rainbow; this keeps them silver.
    iridescence: 0,
    interactive: true,
    theme: 'auto',
  });
  if (!metal) return;                       // no WebGL — keep the old backdrop

  fx.classList.add('metal-on');
  S.metalStop = () => metal.destroy();
})();

connect(); render();
