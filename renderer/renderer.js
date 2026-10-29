/* ─────────────────────────────────────────────────────────────
   MOONWALK – Renderer (Raw Audio Streaming to Backend)
   ───────────────────────────────────────────────────────────── */

const State = Object.freeze({
  IDLE: "IDLE",
  LISTENING: "LISTENING",
  LOADING: "LOADING",
  DOING: "DOING",
  RESPONDING: "RESPONDING"
});

const WS_URL = "ws://127.0.0.1:8000/ws";

/* ── IPC Bridge ── */
const bridge = window.overlayAPI || {
  hideWindow: async () => { },
  enableMouse: () => { },
  disableMouse: () => { },
  onStartListening: () => () => { },
  onOverlayHidden: () => () => { },
  onResetOverlayPosition: () => () => { },
  logError: () => { },
  logInfo: () => { },
};

/* ── IPC Bridge ── */

/* ── DOM Refs ── */
const wrapper = document.getElementById("ui-wrapper");
const uiIdle = document.getElementById("ui-idle");
const uiListening = document.getElementById("ui-listening");
const uiLoading = document.getElementById("ui-loading");
const uiDoing = document.getElementById("ui-doing");
const glow = document.getElementById("glow");
const stageEl = document.querySelector('.stage');
const uiResponse = document.getElementById("ui-response");
const statusEl = document.getElementById("status-text");
const doingTextEl = document.getElementById("doing-text");
const appIconEl = document.getElementById("app-icon");
const typewriterText = document.getElementById("typewriter-text");
const typewriterCursor = document.getElementById("typewriter-cursor");
const responseTextEl = document.getElementById("response-text");
const responseCursorEl = document.getElementById("response-cursor");
const responseDismissEl = document.getElementById("response-dismiss");
const commandPanel = document.getElementById("command-panel");
const commandInput = document.getElementById("command-input");
const commandSend = document.getElementById("command-panel-send");
const commandClose = document.getElementById("command-panel-close");
const pillStopBtn = document.getElementById("pill-stop");

/* ── Modal DOM Refs ── */
const modalRich = document.getElementById("modal-rich");
const richTitle = document.getElementById("rich-title");
const richBody = document.getElementById("rich-body");
const richDismiss = document.getElementById("rich-dismiss");

const modalTable = document.getElementById("modal-table");
const tableTitle = document.getElementById("table-title");
const tableHead = document.getElementById("table-head");
const tableBody = document.getElementById("table-body");
const tableFooter = document.getElementById("table-footer");
const tableDismiss = document.getElementById("table-dismiss");

const modalList = document.getElementById("modal-list");
const listTitle = document.getElementById("list-title");
const listMessage = document.getElementById("list-message");
const listItems = document.getElementById("list-items");
const listDismiss = document.getElementById("list-dismiss");

const modalConfirm = document.getElementById("modal-confirm");
const confirmBody = document.getElementById("confirm-body");
const confirmActions = document.getElementById("confirm-actions");
const confirmDismiss = document.getElementById("confirm-dismiss");

const modalMedia = document.getElementById("modal-media");
const mediaMessage = document.getElementById("media-message");
const mediaImg = document.getElementById("media-img");
const mediaCaption = document.getElementById("media-caption");
const mediaDismiss = document.getElementById("media-dismiss");

const modalSteps = document.getElementById("modal-steps");
const stepsTitle = document.getElementById("steps-title");
const stepsSubtitle = document.getElementById("steps-subtitle");
const stepsMessage = document.getElementById("steps-message");
const stepsTimeline = document.getElementById("steps-timeline");
const stepsActions = document.getElementById("steps-actions");
const stepsDismiss = document.getElementById("steps-dismiss");

const modalProducts = document.getElementById("modal-products");
const productsHeader = document.getElementById("products-header");
const productsTitle = document.getElementById("products-title");
const productsSubtitle = document.getElementById("products-subtitle");
const productsMessage = document.getElementById("products-message");
const productsBody = document.getElementById("products-body");
const productsGrid = document.getElementById("products-grid");
const productsSidebar = document.getElementById("products-sidebar");
const productsDismiss = document.getElementById("products-dismiss");

/* All modal containers for bulk dismiss */
const ALL_MODALS = [uiResponse, modalRich, modalTable, modalList, modalConfirm, modalMedia, modalSteps, modalProducts];

/* ── App State ── */
const app = {
  current: State.IDLE,
  visible: true,
  ws: null,
  reconnectTimer: null,
  reconnectDelay: 700,
  reconnectMaxDelay: 7000,
  mouseEnabled: false,
  isDisposed: false,
  detectedApp: "",
  actionMessage: "Processing...",
  autoResetTimer: null,
  streamTimer: null,       // Character-by-character typing interval
  streamQueue: "",         // Text waiting to be streamed
  streamIndex: 0,          // Current position in stream

  // Audio Streaming Pipeline
  audioStream: null,
  audioContext: null,
  sourceNode: null,
  scriptProcessor: null,

  // Conversation mode
  conversationMode: false,

  // TTS audio playback
  ttsAudioContext: null,
  ttsQueue: [],           // queued { seq, buffer }, kept sorted by seq
  ttsPlaying: false,
  ttsStarting: false,     // guards the decode race (two chunks starting at once)
  ttsCurrentSource: null, // currently playing AudioBufferSourceNode
  _ttsSeen: new Set(),    // seqs already queued — drops duplicate frames
  _ttsNextSeq: 0,         // fallback when the backend omits seq

  // Agent tracking
  agents: {},           // id -> agent state
  runningAgents: 0,
  totalAgents: 0,
  commandPanelOpen: false,
  currentPlanId: null,  // active plan modal correlation id
  _skipAfterModalShow: false,  // Multi-modal flag: skip individual afterModalShow calls
};

/* ── UI State Management ── */
function setIslandState(nextStateClass) {
  wrapper.className = `glass-pill ${nextStateClass}`;
}

function switchContent(target) {
  uiIdle.classList.remove('active');
  uiListening.classList.remove('active');
  uiLoading.classList.remove('active');
  uiDoing.classList.remove('active');
  target.classList.add('active');
}

/** Truncate text to a maximum number of words */
function truncateToWords(text, max = 2) {
  const words = (text || '').trim().split(/\s+/);
  if (words.length <= max) return text.trim();
  return words.slice(0, max).join(' ') + '…';
}

function setState(next, { tier = "", text = null, appName = "", iconUrl = "", force = false, variant = "" } = {}) {
  if (!force && app.current === next) return;
  app.current = next;

  // Clear any previous variant class
  wrapper.classList.remove('variant-browsing', 'variant-typing', 'variant-searching', 'variant-executing', 'variant-planning');

  // Hide response card when switching to non-response states
  if (next !== State.RESPONDING && !uiResponse.classList.contains('dismissing')) {
    dismissResponseCard(true);
  }

  if (next === State.IDLE) {
    glow.classList.remove('active');
    setIslandState('state-idle');
    switchContent(uiIdle);
    statusEl.innerText = "Hey ClarityOS";
    typewriterText.innerText = '';
  }
  else if (next === State.LISTENING) {
    glow.classList.add('active');
    setIslandState('state-listening');
    switchContent(uiListening);
    typewriterText.innerText = "Listening...";
    typewriterCursor.style.display = 'inline-block';
  }
  else if (next === State.LOADING) {
    glow.classList.add('active');
    setIslandState('state-loading');
    switchContent(uiLoading);
  }
  else if (next === State.DOING) {
    glow.classList.add('active');
    setIslandState('state-doing');
    switchContent(uiDoing);

    // Apply variant class for visual differentiation
    if (variant) wrapper.classList.add(`variant-${variant}`);

    if (text) doingTextEl.innerText = truncateToWords(text, 2);

    if (iconUrl) {
      appIconEl.src = iconUrl;
      appIconEl.style.display = 'block';
    } else {
      appIconEl.style.display = 'none';
    }
  }
  else if (next === State.RESPONDING) {
    glow.classList.add('active');
    setIslandState('state-loading');
    switchContent(uiLoading);
  }

  // AI Analysis mode — drives the glassmorphism "active analysis" visuals
  glow.classList.toggle('analyzing', next === State.DOING || next === State.LOADING);
  if (stageEl) stageEl.classList.toggle('analyzing', next === State.DOING);

  // Show stop button when agent is working
  if (pillStopBtn) {
    const working = next === State.DOING || next === State.LOADING;
    pillStopBtn.classList.toggle('hidden', !working);
  }
}

function clearCommandContext() {
  app.detectedApp = "";
  app.actionMessage = "Processing...";
  appIconEl.src = "";
  appIconEl.style.display = 'none';
  app.currentPlanId = null;
}

// ── Tool approval prompt ──────────────────────────────────────
// The backend blocks a consequential tool (phone call, shell, file write…)
// until the user answers. No answer means denied, so this prompt must be
// visible and clickable whenever one arrives.

function dismissApprovalPrompt(approvalId) {
  const existing = document.getElementById('approval-prompt');
  if (!existing) return;
  // Ignore a resolution for some other request that already moved on.
  if (approvalId && existing.dataset.approvalId !== approvalId) return;
  if (app._approvalTimer) {
    clearTimeout(app._approvalTimer);
    app._approvalTimer = null;
  }
  existing.remove();
  app.pendingApprovalId = null;
  if (!app.commandPanelOpen) setMouseEnabled(false);
}

function answerApproval(approvalId, approve) {
  if (app.ws && app.ws.readyState === WebSocket.OPEN) {
    app.ws.send(JSON.stringify({
      type: 'approval_response',
      approval_id: approvalId,
      approve: Boolean(approve),
    }));
  }
  dismissApprovalPrompt(approvalId);
}

function showApprovalPrompt(msg) {
  const approvalId = msg && msg.approval_id;
  if (!approvalId) return;
  dismissApprovalPrompt();            // only ever one prompt on screen

  const box = document.createElement('div');
  box.id = 'approval-prompt';
  box.className = 'approval-prompt';
  box.dataset.approvalId = approvalId;

  const title = document.createElement('div');
  title.className = 'approval-title';
  title.textContent = 'Approve this action?';

  const tool = document.createElement('div');
  tool.className = 'approval-tool';
  tool.textContent = msg.tool || 'unknown tool';

  const reason = document.createElement('div');
  reason.className = 'approval-reason';
  reason.textContent = msg.reason || '';

  // textContent throughout: tool arguments are untrusted input and must never
  // be parsed as HTML.
  const detail = document.createElement('div');
  detail.className = 'approval-detail';
  detail.textContent = msg.summary || '';

  const actions = document.createElement('div');
  actions.className = 'approval-actions';

  const denyBtn = document.createElement('button');
  denyBtn.className = 'approval-btn approval-deny';
  denyBtn.textContent = 'Deny';
  denyBtn.addEventListener('click', () => answerApproval(approvalId, false));

  const allowBtn = document.createElement('button');
  allowBtn.className = 'approval-btn approval-allow';
  allowBtn.textContent = 'Approve';
  allowBtn.addEventListener('click', () => answerApproval(approvalId, true));

  actions.append(denyBtn, allowBtn);
  box.append(title, tool, reason, detail, actions);
  wrapper.appendChild(box);

  app.pendingApprovalId = approvalId;
  setMouseEnabled(true);              // the overlay is click-through otherwise
  denyBtn.focus();

  // Mirror the backend's own expiry so a stale card never lingers. The backend
  // is the authority: if this fires first the user simply loses the chance.
  const timeoutMs = Math.max(5, Number(msg.timeout) || 120) * 1000;
  app._approvalTimer = setTimeout(() => dismissApprovalPrompt(approvalId), timeoutMs);
}

function setMouseEnabled(enabled) {
  const next = Boolean(enabled);
  if (app.mouseEnabled === next) return;
  app.mouseEnabled = next;
  next ? bridge.enableMouse() : bridge.disableMouse();
  document.body.classList.toggle('mouse-enabled', next);
}

function openCommandPanel(prefill = "") {
  if (!commandPanel || !commandInput) return;
  app.commandPanelOpen = true;
  commandPanel.classList.remove("hidden");
  if (prefill) commandInput.value = prefill;
  setMouseEnabled(true);
  requestAnimationFrame(() => {
    commandInput.focus();
    const end = commandInput.value.length;
    commandInput.setSelectionRange(end, end);
  });
}

function closeCommandPanel({ clear = false } = {}) {
  if (!commandPanel || !commandInput) return;
  app.commandPanelOpen = false;
  commandPanel.classList.add("hidden");
  if (clear) commandInput.value = "";
  commandInput.blur();
  setMouseEnabled(false);
}

function submitCommandPanel() {
  if (!commandInput) return;
  const text = (commandInput.value || "").trim();
  if (!text) {
    commandInput.focus();
    return;
  }

  if (!app.ws || app.ws.readyState !== WebSocket.OPEN) {
    showResponseCard("ClarityOS is not connected. Try again in a moment.");
    return;
  }

  app.ws.send(JSON.stringify({
    type: "text_input",
    text,
  }));

  commandInput.value = "";
  closeCommandPanel();
  dismissAllModals(true);
  setState(State.LOADING, { force: true });
}

/* ── Response Card: Streaming Text ── */

/* ── Lightweight Markdown → HTML renderer (with KaTeX math) ── */
function renderMarkdown(text) {
  // ── 0. Extract math blocks before any other processing ──
  // We replace them with unique placeholders so markdown processing
  // (HTML escaping, bold/italic) doesn't mangle LaTeX.
  const mathSlots = [];
  function stashMath(latex, displayMode) {
    const idx = mathSlots.length;
    try {
      mathSlots.push(katex.renderToString(latex, {
        throwOnError: false,
        displayMode: displayMode,
        output: 'html',
      }));
    } catch (_) {
      // Fallback: show the raw LaTeX in a code span
      mathSlots.push(`<code>${latex}</code>`);
    }
    return `\x00MATH${idx}\x00`;
  }

  // Display math: $$...$$  (possibly multi-line)
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_, latex) => stashMath(latex.trim(), true));
  // Inline math: $...$  (single line, non-greedy)
  text = text.replace(/\$([^$\n]+?)\$/g, (_, latex) => stashMath(latex.trim(), false));

  // ── 1. Escape HTML entities ──
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Code blocks (```lang ... ```)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre class="md-code-block"><code>${code.trim()}</code></pre>`;
  });

  // Inline code (`code`)
  html = html.replace(/`([^`]+)`/g, '<code class="md-inline-code">$1</code>');

  // Bold + Italic (***text***)
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');

  // Bold (**text**)
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  // Italic (*text*)
  html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');

  // Headings (### h3, ## h2, # h1) — only at line start
  html = html.replace(/^### (.+)$/gm, '<h4 class="md-heading">$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3 class="md-heading">$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2 class="md-heading">$1</h2>');

  // Horizontal rule (--- or ***)
  html = html.replace(/^(\*{3,}|-{3,})$/gm, '<hr class="md-hr">');

  // Now split into lines for block-level processing (lists & paragraphs)
  const lines = html.split('\n');
  let result = [];
  let inOl = false;
  let inUl = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const olMatch = line.match(/^(\d+)\.\s+(.+)$/);
    const ulMatch = line.match(/^[-*•]\s+(.+)$/);

    if (olMatch) {
      if (!inOl) { result.push('<ol class="md-list">'); inOl = true; }
      if (inUl) { result.push('</ul>'); inUl = false; }
      result.push(`<li>${olMatch[2]}</li>`);
    } else if (ulMatch) {
      if (!inUl) { result.push('<ul class="md-list">'); inUl = true; }
      if (inOl) { result.push('</ol>'); inOl = false; }
      result.push(`<li>${ulMatch[1]}</li>`);
    } else {
      if (inOl) { result.push('</ol>'); inOl = false; }
      if (inUl) { result.push('</ul>'); inUl = false; }
      // Preserve blank lines as spacing, non-blank lines as paragraphs
      if (line.trim() === '') {
        result.push('<div class="md-spacer"></div>');
      } else if (line.startsWith('<h') || line.startsWith('<pre') || line.startsWith('<hr')) {
        result.push(line);
      } else {
        result.push(`<p class="md-paragraph">${line}</p>`);
      }
    }
  }
  if (inOl) result.push('</ol>');
  if (inUl) result.push('</ul>');

  let output = result.join('\n');

  // ── Restore math placeholders → rendered KaTeX HTML ──
  output = output.replace(/\x00MATH(\d+)\x00/g, (_, idx) => mathSlots[parseInt(idx, 10)]);

  return output;
}

function showResponseCard(fullText, awaitInput = false) {
  // Cancel any pending timers
  if (app.streamTimer) {
    clearInterval(app.streamTimer);
    app.streamTimer = null;
  }
  if (app.autoResetTimer) {
    clearTimeout(app.autoResetTimer);
    app.autoResetTimer = null;
  }

  // Reset
  responseTextEl.innerHTML = '';
  responseCursorEl.classList.remove('hidden');

  // Tokenize into words, preserving whitespace & newlines as separate tokens
  const tokens = fullText.match(/\S+|\n| +/g) || [fullText];
  let tokenIdx = 0;
  let visibleText = '';

  // Show card
  uiResponse.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });

  // ── Word-by-word streaming with live markdown formatting ──
  const WORD_DELAY = 35; // ms per token
  app.streamTimer = setInterval(() => {
    if (tokenIdx >= tokens.length) {
      // ── Done ──
      clearInterval(app.streamTimer);
      app.streamTimer = null;
      responseTextEl.innerHTML = renderMarkdown(fullText);
      responseCursorEl.classList.add('hidden');

      if (awaitInput || app.conversationMode) {
        setIslandState('state-listening');
        switchContent(uiListening);
        typewriterText.innerText = "Listening...";
        app.current = State.LISTENING;
        app.autoResetTimer = setTimeout(() => {
          dismissResponseCard();
          setState(State.IDLE, { force: true });
          clearCommandContext();
          app.autoResetTimer = null;
        }, app.conversationMode ? 120000 : 30000);
      } else {
        setIslandState('state-idle');
        switchContent(uiIdle);
        statusEl.innerText = "Hey ClarityOS";
        app.autoResetTimer = setTimeout(() => {
          dismissResponseCard();
          app.current = State.IDLE;
          clearCommandContext();
          app.autoResetTimer = null;
        }, 10000);
      }
      return;
    }

    visibleText += tokens[tokenIdx];
    tokenIdx++;

    // Render the visible portion as formatted markdown
    responseTextEl.innerHTML = renderMarkdown(visibleText);
    uiResponse.scrollTop = uiResponse.scrollHeight;
  }, WORD_DELAY);
}

function dismissResponseCard(instant = false) {
  // Stop any ongoing stream
  if (app.streamTimer) {
    clearInterval(app.streamTimer);
    app.streamTimer = null;
  }

  if (instant || uiResponse.classList.contains('hidden')) {
    uiResponse.classList.add('hidden');
    uiResponse.classList.remove('dismissing');
    return;
  }

  // Animate out
  uiResponse.classList.add('dismissing');
  setTimeout(() => {
    uiResponse.classList.add('hidden');
    uiResponse.classList.remove('dismissing');
  }, 300);
}

// Dismiss button
responseDismissEl.addEventListener('click', () => {
  if (app.autoResetTimer) {
    clearTimeout(app.autoResetTimer);
    app.autoResetTimer = null;
  }
  dismissResponseCard();
  dismissAllModals();
  setState(State.IDLE, { force: true });
  clearCommandContext();
});

// Stop-task button on pill
if (pillStopBtn) {
  pillStopBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    cancelActiveTask();
  });
}

if (commandSend) {
  commandSend.addEventListener("click", () => {
    submitCommandPanel();
  });
}

if (commandClose) {
  commandClose.addEventListener("click", () => {
    closeCommandPanel();
  });
}

if (commandInput) {
  commandInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitCommandPanel();
    }
  });
}

/* ═══════════════════════════════════════════════════════════════
   MODAL SYSTEM — Type-specific renderers
   ═══════════════════════════════════════════════════════════════ */

/** Dismiss every modal and reset to idle */
function dismissAllModals(instant = false) {
  ALL_MODALS.forEach(el => {
    if (instant || el.classList.contains('hidden')) {
      el.classList.add('hidden');
      el.classList.remove('dismissing');
    } else {
      el.classList.add('dismissing');
      setTimeout(() => {
        el.classList.add('hidden');
        el.classList.remove('dismissing');
      }, 300);
    }
  });
  // Restore response dismiss button visibility (hidden during multi-modal stacking)
  responseDismissEl.style.display = '';
}

/** Wire dismiss buttons for all non-text modals */
[richDismiss, tableDismiss, listDismiss, confirmDismiss, mediaDismiss, stepsDismiss, productsDismiss].forEach(btn => {
  if (!btn) return;
  btn.addEventListener('click', () => {
    if (btn === stepsDismiss && modalSteps.classList.contains('mode-plan')) {
      if (app.ws && app.ws.readyState === WebSocket.OPEN) {
        app.ws.send(JSON.stringify({
          type: 'user_action',
          action: 'cancel_plan',
          plan_id: app.currentPlanId || undefined,
        }));
      }
    }
    if (app.autoResetTimer) { clearTimeout(app.autoResetTimer); app.autoResetTimer = null; }
    dismissAllModals();
    setState(State.IDLE, { force: true });
    clearCommandContext();
  });
});

/** Schedule auto-dismiss after a modal is shown */
function scheduleModalAutoDismiss(awaitInput, delayMs) {
  if (app.autoResetTimer) { clearTimeout(app.autoResetTimer); app.autoResetTimer = null; }
  const timeout = delayMs || (awaitInput ? 30000 : 12000);
  app.autoResetTimer = setTimeout(() => {
    dismissAllModals();
    setState(State.IDLE, { force: true });
    clearCommandContext();
    app.autoResetTimer = null;
  }, timeout);
}

/** Modal types that stay on screen until manually dismissed */
const PERSISTENT_MODALS = new Set(['plan', 'confirm']);

/**
 * Master entry-point for all response modals.
 * Called from the WS message handler. Parses the payload and dispatches
 * to the right modal renderer.
 */
function showResponseModal(payload, awaitInput = false) {
  // Dismiss everything first
  dismissAllModals(true);
  dismissResponseCard(true);

  // Parse structured payload — may be a JSON string from the tool
  let data = payload;
  if (typeof payload === 'string') {
    try { data = JSON.parse(payload); } catch { data = { message: payload, modal: 'text' }; }
  }

  // ── Multi-modal: array of stacked modals ──
  if (data.modals && Array.isArray(data.modals)) {
    app._skipAfterModalShow = true;
    let hasPersistent = false;

    data.modals.forEach(modalDef => {
      const type = modalDef.modal || 'text';
      if (PERSISTENT_MODALS.has(type)) hasPersistent = true;
      dispatchModal(modalDef, awaitInput);
    });

    app._skipAfterModalShow = false;
    setState(State.RESPONDING, { force: true });
    afterModalShow(awaitInput || hasPersistent, undefined, hasPersistent);
    return;
  }

  // ── Single modal ──
  dispatchModal(data, awaitInput);
}

/** Route a single modal definition to its renderer */
function dispatchModal(data, awaitInput) {
  const modalType = data.modal || 'text';
  const message = data.message || data.text || '';

  switch (modalType) {
    case 'rich':
      showRichModal(data, awaitInput);
      break;
    case 'table':
      showTableModal(data, awaitInput);
      break;
    case 'list':
      showListModal(data, awaitInput);
      break;
    case 'confirm':
      showConfirmModal(data, awaitInput);
      break;
    case 'media':
      showMediaModal(data, awaitInput);
      break;
    case 'steps':
      showStepsModal(data, awaitInput);
      break;
    case 'plan':
      data._planMode = true;
      showStepsModal(data, true);
      break;
    case 'cards':
    case 'products':
      showProductsModal(data, awaitInput);
      break;
    case 'text':
    default:
      if (app._skipAfterModalShow) {
        // Multi-modal text: show instantly (no streaming), hide its own dismiss
        responseTextEl.innerHTML = renderMarkdown(message);
        responseCursorEl.classList.add('hidden');
        responseDismissEl.style.display = 'none';
        uiResponse.classList.remove('hidden', 'dismissing');
      } else {
        responseDismissEl.style.display = '';
        showResponseCard(message, awaitInput);
      }
      break;
  }
}

/* ── Rich Modal ── */
function showRichModal(data, awaitInput) {
  richTitle.textContent = data.title || '';
  richTitle.style.display = data.title ? '' : 'none';
  richBody.innerHTML = renderMarkdown(data.message || '');

  modalRich.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── Table Modal ── */
function showTableModal(data, awaitInput) {
  tableTitle.textContent = data.title || '';
  tableTitle.style.display = data.title ? '' : 'none';

  // Render header
  tableHead.innerHTML = '';
  if (data.headers && data.headers.length) {
    const tr = document.createElement('tr');
    data.headers.forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      tr.appendChild(th);
    });
    tableHead.appendChild(tr);
  }

  // Render rows
  tableBody.innerHTML = '';
  if (data.rows && data.rows.length) {
    data.rows.forEach(row => {
      const tr = document.createElement('tr');
      (Array.isArray(row) ? row : [row]).forEach(cell => {
        const td = document.createElement('td');
        td.textContent = String(cell);
        tr.appendChild(td);
      });
      tableBody.appendChild(tr);
    });
  }

  // Footer message
  tableFooter.innerHTML = data.message ? renderMarkdown(data.message) : '';
  tableFooter.style.display = data.message ? '' : 'none';

  modalTable.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── List Modal ── */
function showListModal(data, awaitInput) {
  listTitle.textContent = data.title || '';
  listTitle.style.display = data.title ? '' : 'none';

  listMessage.innerHTML = data.message ? renderMarkdown(data.message) : '';
  listMessage.style.display = data.message ? '' : 'none';

  listItems.innerHTML = '';
  (data.items || []).forEach((item, idx) => {
    const card = document.createElement('div');
    card.className = 'modal-list-card';
    // Use explicit icon if provided, otherwise auto-number
    const indicator = item.icon
      ? `<span class="list-card-icon">${escapeHtml(item.icon)}</span>`
      : `<span class="list-card-num">${idx + 1}</span>`;
    card.innerHTML = `
      ${indicator}
      <div class="list-card-body">
        <div class="list-card-title">${escapeHtml(item.title)}</div>
        ${item.description ? `<div class="list-card-desc">${escapeHtml(item.description)}</div>` : ''}
      </div>
    `;
    listItems.appendChild(card);
  });

  modalList.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── Confirm Modal ── */
function showConfirmModal(data, awaitInput) {
  confirmBody.innerHTML = renderMarkdown(data.message || '');

  confirmActions.innerHTML = '';
  (data.actions || []).forEach((action, i) => {
    const btn = document.createElement('button');
    btn.className = i === 0 ? 'modal-confirm-btn primary' : 'modal-confirm-btn secondary';
    btn.textContent = action.label;
    btn.addEventListener('click', () => {
      // Send the choice back via WS
      if (app.ws && app.ws.readyState === WebSocket.OPEN) {
        app.ws.send(JSON.stringify({
          type: "user_action",
          action: action.value
        }));
      }
      dismissAllModals();
      setState(State.LOADING, { force: true });
    });
    confirmActions.appendChild(btn);
  });

  modalConfirm.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  // Confirm modals are persistent — stay until user picks or dismisses
  if (!app._skipAfterModalShow) afterModalShow(true, 60000, true);
}

/* ── Media Modal ── */
function showMediaModal(data, awaitInput) {
  mediaMessage.innerHTML = data.message ? renderMarkdown(data.message) : '';
  mediaMessage.style.display = data.message ? '' : 'none';

  mediaImg.src = data.media_url || '';
  mediaImg.alt = data.caption || 'Image';
  mediaCaption.textContent = data.caption || '';
  mediaCaption.style.display = data.caption ? '' : 'none';

  modalMedia.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 15000);
}

/* ── Steps Modal (dual mode: progress / plan) ── */
function showStepsModal(data, awaitInput) {
  const isPlan = data._planMode || data.steps_mode === 'plan' || data.modal === 'plan';
  const stepsList = data.steps || [];
  app.currentPlanId = isPlan ? (data.plan_id || null) : null;

  // ── Mode class on container ──
  modalSteps.classList.remove('mode-progress', 'mode-plan');
  modalSteps.classList.add(isPlan ? 'mode-plan' : 'mode-progress');

  // ── Title & subtitle ──
  if (isPlan) {
    // Plan modal: headerless — pair with a text bubble above for context
    stepsTitle.style.display = 'none';
    stepsSubtitle.style.display = 'none';
    stepsMessage.style.display = 'none';
  } else {
    stepsTitle.textContent = data.title || 'Task Progress';
    stepsTitle.style.display = '';
    const done = stepsList.filter(s => s.status === 'done').length;
    if (stepsList.length > 0) {
      stepsSubtitle.textContent = `${done} of ${stepsList.length} complete`;
      stepsSubtitle.style.display = '';
    } else {
      stepsSubtitle.style.display = 'none';
    }
  }

  // ── Message (only for progress mode) ──
  if (!isPlan && data.message) {
    stepsMessage.innerHTML = renderMarkdown(data.message);
    stepsMessage.style.display = '';
  } else if (!isPlan) {
    stepsMessage.style.display = 'none';
  }

  // ── Timeline / Plan items ──
  stepsTimeline.innerHTML = '';
  stepsList.forEach((step, i) => {
    const el = document.createElement('div');
    if (isPlan) {
      // Card-row style for plan items
      el.className = 'modal-step-item planned';
      el.style.animationDelay = `${i * 50}ms`;
      el.innerHTML = `
        <div class="step-plan-num">${i + 1}</div>
        <div class="step-body">
          <div class="step-label">${escapeHtml(step.label)}</div>
          ${step.detail ? `<div class="step-detail">${escapeHtml(step.detail)}</div>` : ''}
        </div>
      `;
    } else {
      el.className = `modal-step-item ${step.status}`;
      const icon = step.status === 'done' ? 'OK' : step.status === 'current' ? 'NOW' : '...';
      el.innerHTML = `
        <div class="step-indicator">${icon}</div>
        <div class="step-body">
          <div class="step-label">${escapeHtml(step.label)}</div>
          ${step.detail ? `<div class="step-detail">${escapeHtml(step.detail)}</div>` : ''}
        </div>
      `;
    }
    stepsTimeline.appendChild(el);
  });

  // ── Plan actions ──
  if (isPlan) {
    stepsActions.innerHTML = '';

    const startBtn = document.createElement('button');
    startBtn.className = 'modal-steps-btn-start';
    startBtn.innerHTML = `Proceed
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M5 12h14"/><path d="M12 5l7 7-7 7"/>
      </svg>`;
    startBtn.addEventListener('click', () => {
      if (app.ws && app.ws.readyState === WebSocket.OPEN) {
        app.ws.send(JSON.stringify({
          type: 'user_action',
          action: 'approve_plan',
          plan_id: app.currentPlanId || undefined,
        }));
      }
      dismissAllModals();
      setState(State.LOADING, { force: true });
    });

    stepsActions.appendChild(startBtn);
    stepsActions.classList.remove('hidden');
  } else {
    stepsActions.classList.add('hidden');
  }

  modalSteps.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });

  if (!app._skipAfterModalShow) {
    if (isPlan) {
      // Plan is persistent — stays until user approves, modifies, or cancels
      afterModalShow(true, undefined, true);
    } else {
      afterModalShow(awaitInput, 15000);
    }
  }
}

/* ── Cards Modal (general-purpose image+text cards) ── */
function showProductsModal(data, awaitInput) {
  // Accept both 'cards' (new) and 'products' (legacy) item arrays
  const items = data.cards || data.products || [];

  // ── Header ──
  if (data.title) {
    productsTitle.textContent = data.title;
    productsTitle.style.display = '';
  } else {
    productsTitle.style.display = 'none';
  }
  if (items.length > 0 && data.subtitle) {
    productsSubtitle.textContent = data.subtitle;
    productsSubtitle.style.display = '';
  } else if (items.length > 0) {
    productsSubtitle.textContent = `${items.length} result${items.length !== 1 ? 's' : ''}`;
    productsSubtitle.style.display = '';
  } else {
    productsSubtitle.style.display = 'none';
  }

  // ── Message ──
  productsMessage.innerHTML = data.message ? renderMarkdown(data.message) : '';
  productsMessage.style.display = data.message ? '' : 'none';

  // ── Sidebar (removed — keep it hidden) ──
  modalProducts.classList.remove('has-sidebar');
  productsSidebar.classList.add('hidden');

  // ── Cards ──
  productsGrid.innerHTML = '';
  items.forEach((item, idx) => {
    const card = document.createElement('div');
    card.className = 'card-item';
    card.style.animationDelay = `${idx * 50}ms`;
    if (item.url || item.link) card.classList.add('has-link');

    // ── Image ──
    let imgHtml = '';
    if (item.image) {
      imgHtml = `<div class="card-img-wrap"><img class="card-img" src="${escapeHtml(item.image)}" alt="${escapeHtml(item.name || item.title || '')}" loading="lazy" /></div>`;
    }

    // ── Title ──
    const title = item.name || item.title || '';

    // ── Description ──
    let descHtml = '';
    if (item.description) {
      descHtml = `<div class="card-desc">${escapeHtml(item.description)}</div>`;
    }

    // ── Meta line: price, rating, source ──
    let metaParts = [];
    if (item.price) {
      const priceClass = item.original_price ? 'card-price on-sale' : 'card-price';
      let priceStr = `<span class="${priceClass}">${escapeHtml(item.price)}</span>`;
      if (item.original_price) {
        priceStr += `<span class="card-price-original">${escapeHtml(item.original_price)}</span>`;
      }
      metaParts.push(priceStr);
    }
    if (item.rating != null) {
      const full = Math.floor(item.rating);
      const half = item.rating % 1 >= 0.5 ? 1 : 0;
      const empty = 5 - full - half;
      let stars = '<span class="card-stars">'
        + '★'.repeat(full) + (half ? '⯨' : '') + '<span class="star-empty">' + '★'.repeat(empty) + '</span>'
        + `</span> <span class="card-rating-num">${item.rating}</span>`;
      if (item.reviews) stars += ` <span class="card-reviews">(${escapeHtml(String(item.reviews))})</span>`;
      metaParts.push(stars);
    }
    if (item.source) {
      metaParts.push(`<span class="card-source">${escapeHtml(item.source)}</span>`);
    }
    const metaHtml = metaParts.length ? `<div class="card-meta">${metaParts.join('<span class="card-meta-sep">·</span>')}</div>` : '';

    // ── Link / CTA ──
    const linkUrl = item.url || item.link || '';
    let linkHtml = '';
    if (linkUrl) {
      const linkLabel = item.link_label || 'View';
      linkHtml = `<div class="card-link" data-url="${escapeHtml(linkUrl)}">
        <span>${escapeHtml(linkLabel)}</span>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </div>`;
    }

    card.innerHTML = `
      ${imgHtml}
      <div class="card-body">
        <div class="card-title">${escapeHtml(title)}</div>
        ${descHtml}
        ${metaHtml}
        ${linkHtml}
      </div>
    `;

    // Link click handler
    const linkEl = card.querySelector('.card-link');
    if (linkEl) {
      linkEl.addEventListener('click', (e) => {
        e.stopPropagation();
        if (app.ws && app.ws.readyState === WebSocket.OPEN) {
          app.ws.send(JSON.stringify({ type: 'open_url', url: linkUrl }));
        }
      });
    }

    // Whole-card click if has link
    if (linkUrl) {
      card.addEventListener('click', () => {
        if (app.ws && app.ws.readyState === WebSocket.OPEN) {
          app.ws.send(JSON.stringify({ type: 'open_url', url: linkUrl }));
        }
      });
    }

    productsGrid.appendChild(card);
  });

  modalProducts.classList.remove('hidden', 'dismissing');
  setState(State.RESPONDING, { force: true });
  if (!app._skipAfterModalShow) afterModalShow(awaitInput, 20000);
}

/** Shared post-show: set island state + schedule dismiss
 *  persistent = true → modal stays until manually dismissed (no timer) */
function afterModalShow(awaitInput, defaultTimeout, persistent = false) {
  if (awaitInput || app.conversationMode) {
    setIslandState('state-listening');
    switchContent(uiListening);
    typewriterText.innerText = "Listening...";
    app.current = State.LISTENING;
    if (!persistent) scheduleModalAutoDismiss(true, app.conversationMode ? 120000 : 30000);
  } else {
    setIslandState('state-idle');
    switchContent(uiIdle);
    statusEl.innerText = "Hey ClarityOS";
    if (!persistent) scheduleModalAutoDismiss(false, defaultTimeout);
  }
}

/* ── Audio Encoding (PCM to Base64 WAV) ── */

function floatTo16BitPCM(output, offset, input) {
  for (let i = 0; i < input.length; i++, offset += 2) {
    // Clamp between -1 and 1
    const s = Math.max(-1, Math.min(1, input[i]));
    // Convert to 16-bit integer (multiply by 0x7FFF)
    output.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
}

function writeString(view, offset, string) {
  for (let i = 0; i < string.length; i++) {
    view.setUint8(offset + i, string.charCodeAt(i));
  }
}

// Packages the raw Float32Array PCM chunk into a full WAV file Buffer
function encodeWAVChunk(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  // RIFF chunk descriptor
  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(view, 8, 'WAVE');

  // fmt sub-chunk
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);             // Subchunk1Size (16 for PCM)
  view.setUint16(20, 1, true);              // AudioFormat (1 for PCM)
  view.setUint16(22, 1, true);              // NumChannels (1: mono)
  view.setUint32(24, sampleRate, true);     // SampleRate
  view.setUint32(28, sampleRate * 2, true); // ByteRate (SampleRate * NumChannels * BitsPerSample/8)
  view.setUint16(32, 2, true);              // BlockAlign (NumChannels * BitsPerSample/8)
  view.setUint16(34, 16, true);             // BitsPerSample

  // data sub-chunk
  writeString(view, 36, 'data');
  view.setUint32(40, samples.length * 2, true);

  // Write the PCM samples
  floatTo16BitPCM(view, 44, samples);

  return buffer;
}

function arrayBufferToBase64(buffer) {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  const len = bytes.byteLength;
  for (let i = 0; i < len; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return window.btoa(binary);
}

/* ── Continuous Microphone Streaming ── */

async function startAudioStreaming() {
  if (app.audioStream) return; // Already running

  try {
    app.audioStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
    app.audioContext = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 16000 // Force 16kHz for backend speech models
    });

    // We use ScriptProcessorNode because it's the easiest cross-platform way 
    // to access raw PCM data without AudioWorklet complexity.
    app.sourceNode = app.audioContext.createMediaStreamSource(app.audioStream);
    // Higher frequency chunks for lower latency (1024 samples @ 16kHz = 64ms)
    app.scriptProcessor = app.audioContext.createScriptProcessor(1024, 1, 1);

    app.scriptProcessor.onaudioprocess = (event) => {
      // Only send if websocket is open
      if (!app.ws || app.ws.readyState !== WebSocket.OPEN) return;

      const inputBuffer = event.inputBuffer.getChannelData(0); // Mono Float32Array
      const sampleRate = app.audioContext.sampleRate; // Typically 16000 here

      // Pack into a WAV wrapper
      const wavBuffer = encodeWAVChunk(inputBuffer, sampleRate);

      // Convert to base64
      const base64Audio = arrayBufferToBase64(wavBuffer);

      // Stream to Python Backend
      app.ws.send(JSON.stringify({
        type: "audio_chunk",
        payload: base64Audio
      }));
    };

    app.sourceNode.connect(app.scriptProcessor);
    app.scriptProcessor.connect(app.audioContext.destination);

    console.log("Started continuous audio streaming at 16kHz");
  } catch (err) {
    console.error("Failed to access microphone:", err);
    statusEl.innerText = "Mic Error";
    if (bridge.logError) {
      bridge.logError(`Mic Access Failed: ${err.message}`);
    }
  }
}

async function stopAudioStreaming() {
  if (app.scriptProcessor) {
    app.scriptProcessor.disconnect();
    app.scriptProcessor = null;
  }
  if (app.sourceNode) {
    app.sourceNode.disconnect();
    app.sourceNode = null;
  }
  if (app.audioContext) {
    await app.audioContext.close();
    app.audioContext = null;
  }
  if (app.audioStream) {
    app.audioStream.getTracks().forEach(track => track.stop());
    app.audioStream = null;
  }
}


/* ── TTS Audio Playback ── */

function getTTSContext() {
  if (!app.ttsAudioContext || app.ttsAudioContext.state === 'closed') {
    app.ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
  }
  // Chromium creates an AudioContext in the "suspended" state until a user
  // gesture resumes it. Without this the chunks decode fine and the source
  // starts, but nothing is ever audible.
  if (app.ttsAudioContext.state === 'suspended') {
    app.ttsAudioContext.resume().catch((err) => {
      console.warn('[TTS] Could not resume audio context:', err);
    });
  }
  return app.ttsAudioContext;
}

async function handleTTSChunk(data) {
  const seq = Number.isFinite(data.seq) ? data.seq : app._ttsNextSeq++;

  // Ignore a chunk we have already taken. Without this a duplicated or
  // re-delivered websocket frame would be queued twice and spoken twice.
  if (app._ttsSeen.has(seq)) {
    console.warn('[TTS] duplicate chunk seq=' + seq + ' ignored');
    return;
  }
  app._ttsSeen.add(seq);

  try {
    const audioBytes = Uint8Array.from(atob(data.data), c => c.charCodeAt(0));
    const ctx = getTTSContext();
    const audioBuffer = await ctx.decodeAudioData(audioBytes.buffer.slice(0));

    // decodeAudioData is async and decode time varies, so chunks can finish
    // out of order. Insert by seq to keep sentences in the order they were
    // synthesized.
    const item = { seq, buffer: audioBuffer };
    let i = app.ttsQueue.length;
    while (i > 0 && app.ttsQueue[i - 1].seq > seq) i--;
    app.ttsQueue.splice(i, 0, item);

    // `ttsStarting` closes the race where two concurrent decodes both see
    // ttsPlaying === false and each start a source — which played two
    // overlapping voices at once.
    if (!app.ttsPlaying && !app.ttsStarting) {
      app.ttsStarting = true;
      playNextTTSChunk();
    }
  } catch (err) {
    console.error('[TTS] Decode error:', err);
  }
}

function playNextTTSChunk() {
  if (app.ttsQueue.length === 0) {
    app.ttsPlaying = false;
    app.ttsStarting = false;
    app.ttsCurrentSource = null;
    app._ttsSeen.clear();          // next reply starts a fresh seq window
    app._ttsNextSeq = 0;
    // Remove speaking variant
    wrapper.classList.remove('variant-speaking');
    // Notify backend TTS playback is done. This is what releases the backend's
    // mic gate, so it must be sent exactly when the speakers go quiet.
    if (app.ws && app.ws.readyState === WebSocket.OPEN) {
      app.ws.send(JSON.stringify({ type: 'tts_done' }));
    }
    return;
  }

  app.ttsPlaying = true;
  wrapper.classList.add('variant-speaking');

  const ctx = getTTSContext();

  // A suspended context accepts start() but produces no sound, and onended
  // never fires — which stalls the queue and silently drops the rest of the
  // reply. Wait for it to actually run before scheduling anything.
  if (ctx.state !== 'running') {
    ctx.resume().then(() => {
      if (ctx.state === 'running') { startNextBuffer(ctx); return; }
      reportAudioBlocked();
      app.ttsQueue = [];
      app.ttsPlaying = false;
      wrapper.classList.remove('variant-speaking');
    }).catch((err) => {
      console.error('[TTS] resume failed:', err);
      reportAudioBlocked();
      app.ttsQueue = [];
      app.ttsPlaying = false;
      wrapper.classList.remove('variant-speaking');
    });
    return;
  }

  startNextBuffer(ctx);
}

function startNextBuffer(ctx) {
  const item = app.ttsQueue.shift();
  if (!item) { playNextTTSChunk(); return; }
  // Only ever one live source: stop any stragglers before starting the next.
  if (app.ttsCurrentSource) {
    try { app.ttsCurrentSource.onended = null; app.ttsCurrentSource.stop(); } catch {}
    app.ttsCurrentSource = null;
  }
  app.ttsStarting = false;
  const source = ctx.createBufferSource();
  source.buffer = item.buffer;
  source.connect(ctx.destination);
  source.onended = () => playNextTTSChunk();
  source.start(0);
  app.ttsCurrentSource = source;
}

// Say it once, in the UI — a console warning alone looks like working audio.
function reportAudioBlocked() {
  if (app._audioBlockedShown) return;
  app._audioBlockedShown = true;
  console.error('[TTS] AudioContext could not start — spoken replies are muted.');
  try {
    showResponseCard("I can't play audio yet — click the ClarityOS window once, then ask again.");
  } catch (_) { /* card unavailable this early */ }
}

function stopTTS() {
  app.ttsQueue = [];
  if (app.ttsCurrentSource) {
    // Drop onended first, or stop() would trigger playNextTTSChunk and restart.
    try { app.ttsCurrentSource.onended = null; app.ttsCurrentSource.stop(); } catch {}
    app.ttsCurrentSource = null;
  }
  app.ttsPlaying = false;
  app.ttsStarting = false;
  app._ttsSeen.clear();
  app._ttsNextSeq = 0;
  wrapper.classList.remove('variant-speaking');
  // Speakers are silent now — tell the backend so the mic gate opens.
  if (app.ws && app.ws.readyState === WebSocket.OPEN) {
    app.ws.send(JSON.stringify({ type: 'tts_done' }));
  }
}

/* ── Cancel / Stop Active Task ── */

function cancelActiveTask() {
  if (!app.ws || app.ws.readyState !== WebSocket.OPEN) return;
  app.ws.send(JSON.stringify({ type: 'cancel_task' }));
  stopTTS();
}

/* ── Conversation Mode Toggle ── */

function toggleConversationMode() {
  if (!app.ws || app.ws.readyState !== WebSocket.OPEN) return;
  app.conversationMode = !app.conversationMode;
  app.ws.send(JSON.stringify({ type: 'toggle_conversation_mode' }));
}


/* ── WebSocket ── */

function scheduleReconnect() {
  // An inactive presentation must not reconnect: only the mode the user chose
  // holds a backend socket, so one user never gets two agents.
  if (app.isDisposed || app.uiInactive || app.reconnectTimer) return;
  app.reconnectTimer = window.setTimeout(() => {
    app.reconnectTimer = null;
    if (!app.isDisposed) connectWebSocket();
  }, app.reconnectDelay);
  app.reconnectDelay = Math.min(Math.round(app.reconnectDelay * 1.6), app.reconnectMaxDelay);
}

function connectWebSocket() {
  if (app.isDisposed || app.uiInactive) return;
  if (app.ws && (app.ws.readyState === WebSocket.OPEN || app.ws.readyState === WebSocket.CONNECTING)) return;

  try {
    app.ws = new WebSocket(WS_URL);
    app.ws.addEventListener("open", () => {
      app.reconnectDelay = 700;
      statusEl.innerText = "Hey ClarityOS";
      // Clear the unavailable state once the backend is back.
      if (wrapper.classList.contains('state-offline')) setIslandState('state-idle');
      // (Re-)start audio streaming now that the WS is connected.
      // This handles the case where getUserMedia failed at boot (before
      // mic permission was granted during onboarding) or where the WS
      // reconnected after a disconnect.
      startAudioStreaming();
    });

    app.ws.addEventListener("message", (event) => {
      if (typeof event.data !== "string") return;
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }

      console.log("[WS] Received:", msg);

      // ── Agent message types ──

      // 1. "thinking" — Agent is reasoning (show bouncing dots)
      if (msg.type === "thinking" || msg.type === "progress" || msg.state === "state-loading") {
        setState(State.LOADING, { force: true });
        return;
      }

      // 2. "doing" — Agent is executing a tool (show spinner + action text)
      if (msg.type === "doing") {
        // Cancel any pending auto-reset so sequential tool steps show properly
        if (app.autoResetTimer) {
          clearTimeout(app.autoResetTimer);
          app.autoResetTimer = null;
        }
        setState(State.DOING, {
          text: msg.text || "Working...",
          appName: msg.app || "",
          iconUrl: msg.icon_url || "",
          variant: msg.variant || "",
          force: true
        });
        return;
      }

      // 3. "response" — Agent finished, show final answer (routed through modal system)
      if (msg.type === "response" || msg.type === "action") {
        let payload = msg.payload || {};
        

        
        const text = payload.text || payload.message || "Done!";
        const awaitInput = payload.await_input || false;
        app.detectedApp = payload.app || "";

        // If payload contains structured modal data, route through modal system
        if (payload.modal_data || payload.modal) {
          showResponseModal(payload.modal_data || payload, awaitInput);
        } else {
          // Legacy: plain text → default text card
          showResponseCard(text, awaitInput);
        }
        return;
      }

      // 3a. "ack" — Quick acknowledgment before work starts
      if (msg.type === "ack") {
        const ackText = msg.text || "On it";
        // Briefly show ack text in the pill during loading transition
        doingTextEl.innerText = ackText;
        setState(State.DOING, { text: ackText, variant: "", force: true });
        // Auto-transition to loading after a beat
        setTimeout(() => {
          if (app.current === State.DOING && doingTextEl.innerText === ackText) {
            setState(State.LOADING, { force: true });
          }
        }, 800);
        return;
      }

      // 3a-bis. Approval requests — a consequential tool is waiting on the user.
      if (msg.type === "approval_request") {
        showApprovalPrompt(msg);
        return;
      }
      if (msg.type === "approval_resolved") {
        dismissApprovalPrompt(msg.approval_id);
        return;
      }
      if (msg.type === "tool_blocked") {
        dismissApprovalPrompt();
        return;
      }

      // 3b. TTS audio streaming
      if (msg.type === "tts_chunk") {
        handleTTSChunk(msg);
        return;
      }
      if (msg.type === "tts_stop") {
        stopTTS();
        return;
      }
      if (msg.type === "tts_done" && !app.ttsPlaying) {
        // Backend says TTS is done sending — queue is already empty
        return;
      }

      // 3c. "conversation_mode" — Toggle persistent listening mode
      if (msg.type === "conversation_mode") {
        app.conversationMode = !!msg.enabled;
        return;
      }

      // 4. "status" — Direct state transitions (idle, listening, etc.)
      const stateStr = msg.state || (msg.type === "status" ? msg.state : null);
      if (stateStr) {
        if (app.autoResetTimer) {
          clearTimeout(app.autoResetTimer);
          app.autoResetTimer = null;
        }
        const nextState = State[stateStr.toUpperCase().replace("STATE-", "")];
        if (nextState) {
          setState(nextState, { force: true });
          if (nextState === State.IDLE) clearCommandContext();
        }
      }

    });

    app.ws.addEventListener("error", (err) => {
      console.error("[WS] Connection Error:", err);
      if (bridge.logError) {
        bridge.logError("WebSocket connection failed to ws://127.0.0.1:8000/ws");
      }
    });

    app.ws.addEventListener("close", (e) => {
      console.warn("[WS] Connection Closed:", e.code, e.reason);
      if (bridge.logError) {
        bridge.logError(`WebSocket closed: ${e.code} ${e.reason}`);
      }
      // Show the assistant as unavailable rather than idle — idle implies it
      // is listening, which it cannot be with the backend gone.
      if (statusEl) statusEl.innerText = "Reconnecting…";
      setIslandState('state-offline');
      scheduleReconnect();
    });
  } catch {
    scheduleReconnect();
  }
}

/* ── Events ── */
// Hit-test: check if mouse is over any interactive element
function isOverInteractive(event) {
  // Onboarding is a full-screen modal — always interactive while visible
  if (onboardingOverlay && !onboardingOverlay.classList.contains("hidden")) return true;

  const x = event.clientX;
  const y = event.clientY;
  const rects = [wrapper.getBoundingClientRect()];
  if (app.commandPanelOpen && commandPanel && !commandPanel.classList.contains("hidden")) {
    rects.push(commandPanel.getBoundingClientRect());
  }
  // Check response card if visible
  if (!uiResponse.classList.contains('hidden')) rects.push(uiResponse.getBoundingClientRect());
  // Check all modal types
  ALL_MODALS.forEach(m => {
    if (!m.classList.contains('hidden')) rects.push(m.getBoundingClientRect());
  });

  return rects.some(r => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom);
}

document.addEventListener("mousemove", (event) => {
  // While dragging the pill the cursor routinely leaves its bounds; handing the
  // mouse back to the desktop mid-drag would drop the gesture.
  if (dragState) return;
  if (!app.visible) return setMouseEnabled(false);
  setMouseEnabled(isOverInteractive(event));
});

/* ── Moving the overlay ──
   The window is a full-screen transparent sheet, so there is no window to drag
   — moving it means translating the stage. Pill, command panel and response
   card are all children of it, so they travel together, and the click-through
   hit test reads getBoundingClientRect(), which already accounts for the
   transform. */
const OVERLAY_POS_KEY = "clarityos.overlay.offset";
const DRAG_THRESHOLD_PX = 4;
const EDGE_MARGIN_PX = 8;

const overlayOffset = { x: 0, y: 0 };
let dragState = null;

function applyOverlayOffset() {
  if (stageEl) stageEl.style.transform = `translate(${overlayOffset.x}px, ${overlayOffset.y}px)`;
}

// Never let the pill be dragged (or restored) somewhere it cannot be grabbed
// back from — a saved position from a larger monitor must not strand it.
function clampOffset(x, y) {
  if (!wrapper) return { x, y };
  const rect = wrapper.getBoundingClientRect();
  const baseLeft = rect.left - overlayOffset.x;
  const baseTop = rect.top - overlayOffset.y;
  return {
    x: Math.min(Math.max(x, EDGE_MARGIN_PX - baseLeft), window.innerWidth - EDGE_MARGIN_PX - rect.width - baseLeft),
    y: Math.min(Math.max(y, EDGE_MARGIN_PX - baseTop), window.innerHeight - EDGE_MARGIN_PX - rect.height - baseTop),
  };
}

function setOverlayOffset(x, y, { persist = true } = {}) {
  const next = clampOffset(x, y);
  overlayOffset.x = next.x;
  overlayOffset.y = next.y;
  applyOverlayOffset();
  if (persist) {
    try {
      localStorage.setItem(OVERLAY_POS_KEY, JSON.stringify(next));
    } catch (_) {
      // Storage disabled — the position just will not survive a restart.
    }
  }
}

function resetOverlayPosition() {
  setOverlayOffset(0, 0);
}

function restoreOverlayPosition() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(OVERLAY_POS_KEY) || "null");
  } catch (_) {
    saved = null;
  }
  if (!saved || !Number.isFinite(saved.x) || !Number.isFinite(saved.y)) return;
  overlayOffset.x = saved.x;
  overlayOffset.y = saved.y;
  applyOverlayOffset();
  // Re-clamp once layout has settled, in case the display got smaller.
  requestAnimationFrame(() => setOverlayOffset(overlayOffset.x, overlayOffset.y, { persist: false }));
}

if (wrapper) {
  wrapper.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    // Controls inside the pill keep their own behaviour.
    if (event.target.closest("button, input, textarea, a")) return;
    dragState = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: overlayOffset.x,
      originY: overlayOffset.y,
      moved: false,
    };
    setMouseEnabled(true);
    try {
      wrapper.setPointerCapture(event.pointerId);
    } catch (_) {}
  });

  wrapper.addEventListener("pointermove", (event) => {
    if (!dragState || event.pointerId !== dragState.pointerId) return;
    const dx = event.clientX - dragState.startX;
    const dy = event.clientY - dragState.startY;
    if (!dragState.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
    if (!dragState.moved) {
      dragState.moved = true;
      document.body.classList.add("dragging-overlay");
    }
    setOverlayOffset(dragState.originX + dx, dragState.originY + dy, { persist: false });
  });

  const endDrag = (event) => {
    if (!dragState || event.pointerId !== dragState.pointerId) return;
    const wasDragged = dragState.moved;
    try {
      wrapper.releasePointerCapture(event.pointerId);
    } catch (_) {}
    dragState = null;
    document.body.classList.remove("dragging-overlay");

    if (wasDragged) {
      setOverlayOffset(overlayOffset.x, overlayOffset.y); // persist where it landed
      return;
    }
    // Not a drag — a plain click. Same entry point as the global hotkey, so the
    // pill is finally usable without remembering a shortcut.
    if (app.commandPanelOpen) {
      closeCommandPanel();
    } else {
      dismissAllModals(true);
      openCommandPanel();
    }
  };

  wrapper.addEventListener("pointerup", endDrag);
  wrapper.addEventListener("pointercancel", endDrag);
}

window.addEventListener("resize", () => {
  if (overlayOffset.x || overlayOffset.y) {
    setOverlayOffset(overlayOffset.x, overlayOffset.y, { persist: false });
  }
});

bridge.onResetOverlayPosition?.(() => resetOverlayPosition());

restoreOverlayPosition();

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    // If agent is working, cancel the task
    if (app.current === State.DOING || app.current === State.LOADING) {
      cancelActiveTask();
      setState(State.IDLE, { force: true });
      return;
    }
    // If TTS is playing, stop it
    if (app.ttsPlaying) {
      stopTTS();
      return;
    }
    if (app.commandPanelOpen) return closeCommandPanel();
    return bridge.hideWindow();
  }
});

// Global shortcut opens the text command panel for direct prompting.
bridge.onStartListening(() => {
  dismissAllModals(true);
  setState(State.IDLE, { force: true });
  openCommandPanel();
});

bridge.onOverlayHidden(async () => {
  clearCommandContext();
  closeCommandPanel({ clear: true });
  app.visible = true;
  wrapper.classList.remove("hidden");
  setMouseEnabled(false);
  setState(State.IDLE, { force: true });
});

window.addEventListener("beforeunload", async () => {
  app.isDisposed = true;
  await stopAudioStreaming();
  if (app.reconnectTimer) clearTimeout(app.reconnectTimer);
  if (app.ws && app.ws.readyState <= WebSocket.OPEN) app.ws.close();
});

/* ── Init ── */

// ── Welcome Screen ──
// Shown on launch. It never blocks on API keys: the backend may already be
// configured through backend/.env or Windows Credential Manager, and the local
// Windows voice needs no credentials at all. Keys live in Settings, optional.
const onboardingOverlay = document.getElementById("onboarding-overlay");
const welcomeView = document.getElementById("welcome-view");
const settingsView = document.getElementById("settings-view");
const onboardingVersion = document.getElementById("onboarding-version");

const welcomeStatusEl = document.getElementById("welcome-status");
const welcomeStatusText = document.getElementById("welcome-status-text");
const welcomeLaunchBtn = document.getElementById("welcome-launch");

// ── Converging sound-wave field ──
// Lines flow inward from the edges toward the emblem. Pure canvas, no assets.
let welcomeAnim = null;

function startWelcomeCanvas() {
  const canvas = document.getElementById("welcome-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let w = 0, h = 0, cx = 0, cy = 0;

  const resize = () => {
    w = canvas.clientWidth; h = canvas.clientHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = w / 2; cy = h / 2;
  };
  resize();
  window.addEventListener("resize", resize, { passive: true });

  const COUNT = reduced ? 24 : 64;
  const TAU = Math.PI * 2;
  const rays = [];
  for (let i = 0; i < COUNT; i++) {
    rays.push({
      angle: (i / COUNT) * TAU + Math.random() * 0.06,
      speed: 0.0016 + Math.random() * 0.0028,
      phase: Math.random() * TAU,
      amp: 3 + Math.random() * 8,        // gentle sound-wave ripple, not a squiggle
      freq: 1.2 + Math.random() * 1.8,
      hue: Math.random() < 0.5 ? 258 : (Math.random() < 0.5 ? 232 : 199),
      t: Math.random(),
    });
  }

  let raf = 0;
  const draw = (now) => {
    ctx.clearRect(0, 0, w, h);
    const reach = Math.max(w, h) * 0.62;
    // Rays stop short of the centre so they never cross the headline.
    const innerR = Math.min(w, h) * 0.34;

    for (const r of rays) {
      r.t += r.speed;
      if (r.t > 1) r.t -= 1;

      // Travel from the rim inward; nearer the centre = brighter and tighter.
      const head = 1 - r.t;
      const cos = Math.cos(r.angle), sin = Math.sin(r.angle);
      const steps = 26;

      ctx.beginPath();
      for (let s = 0; s <= steps; s++) {
        const p = s / steps;
        const dist = innerR + (reach - innerR) * (head + p * 0.22);
        if (dist > reach) continue;
        const wobble = Math.sin(p * r.freq * TAU + r.phase + now * 0.0011) * r.amp * (1 - head * 0.55);
        const x = cx + cos * dist - sin * wobble;
        const y = cy + sin * dist + cos * wobble;
        if (s === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }

      // Fade in from the rim, dissolve again before reaching the copy.
      const fade = Math.min(1, (1 - head) * 2.2) * Math.min(1, head * 4) * 0.85;
      ctx.strokeStyle = "hsla(" + r.hue + ", 88%, 70%, " + fade.toFixed(3) + ")";
      ctx.lineWidth = 0.6 + (1 - head) * 1.3;
      ctx.stroke();
    }

    raf = requestAnimationFrame(draw);
  };

  if (reduced) {
    draw(0);                       // one static frame
    cancelAnimationFrame(raf);
  } else {
    raf = requestAnimationFrame(draw);
  }

  welcomeAnim = () => { cancelAnimationFrame(raf); window.removeEventListener("resize", resize); };
}

// ── Real runtime status ──
// "starting" until the backend answers, "ready" once the WebSocket is open,
// "unavailable" if it never comes up. Never claims more than it knows.
function setWelcomeStatus(state, text) {
  if (!welcomeStatusEl) return;
  welcomeStatusEl.classList.remove("ready", "failed");
  if (state === "ready") welcomeStatusEl.classList.add("ready");
  if (state === "failed") welcomeStatusEl.classList.add("failed");
  if (welcomeStatusText) welcomeStatusText.textContent = text;
}

async function trackBackendReadiness() {
  setWelcomeStatus("starting", "Starting assistant…");

  // startBackend() reuses an already-running backend, so this cannot
  // double-spawn the Python process.
  let started = { ok: false };
  try {
    started = await bridge.startBackend?.() ?? { ok: false };
  } catch {
    started = { ok: false };
  }

  for (let i = 0; i < 40; i++) {
    if (app.ws && app.ws.readyState === WebSocket.OPEN) {
      setWelcomeStatus("ready", "Assistant ready");
      return true;
    }
    await new Promise((r) => setTimeout(r, 500));
  }

  setWelcomeStatus(
    "failed",
    started.ok ? "Assistant not responding — you can still open Settings"
               : "Assistant unavailable — check the console"
  );
  return false;
}

async function runOnboarding() {
  if (!onboardingOverlay) return;

  const version = await bridge.getVersion?.() ?? "1.0.0";
  if (onboardingVersion) onboardingVersion.textContent = "v" + version;

  setMouseEnabled(true);
  onboardingOverlay.classList.remove("hidden");
  startWelcomeCanvas();
  trackBackendReadiness();
  welcomeLaunchBtn?.focus();
}

// ── Launch ──
// The click is also a genuine user gesture, which is what Chromium requires to
// resume a suspended AudioContext — so spoken replies can play afterwards.
function enterMainView() {
  try { getTTSContext(); } catch (_) { /* audio stays unavailable; not fatal */ }

  onboardingOverlay?.classList.add("hidden");
  if (welcomeAnim) { welcomeAnim(); welcomeAnim = null; }
  setMouseEnabled(false);
  setState(State.IDLE, { force: true });

  if (!app.ws || app.ws.readyState > WebSocket.OPEN) connectWebSocket();
}

welcomeLaunchBtn?.addEventListener("click", enterMainView);

// ── Settings ──
const settingsOpenBtn = document.getElementById("welcome-settings-open");
const settingsCloseBtn = document.getElementById("welcome-settings-close");
const settingsSaveBtn = document.getElementById("welcome-settings-save");
const settingsSavedEl = document.getElementById("settings-saved");
const geminiKeyInput = document.getElementById("onboarding-gemini-key");
const picovoiceKeyInput = document.getElementById("onboarding-picovoice-key");
const picovoiceChip = document.getElementById("picovoice-chip");

function showView(which) {
  welcomeView?.classList.toggle("active", which === "welcome");
  settingsView?.classList.toggle("active", which === "settings");
}

settingsOpenBtn?.addEventListener("click", async () => {
  showView("settings");
  // Reflect what is already configured — without ever revealing a stored value.
  try {
    const creds = await bridge.loadCredentials?.() ?? {};
    if (geminiKeyInput && creds.gemini_api_key) {
      geminiKeyInput.placeholder = "Configured — leave blank to keep";
    }
    if (picovoiceKeyInput && creds.picovoice_key) {
      picovoiceKeyInput.placeholder = "Configured — leave blank to keep";
      if (picovoiceChip) { picovoiceChip.textContent = "configured"; picovoiceChip.classList.add("ok"); }
    }
  } catch { /* credential store unavailable; the fields still work */ }
});

settingsCloseBtn?.addEventListener("click", () => showView("welcome"));

settingsSaveBtn?.addEventListener("click", async () => {
  const gemini = geminiKeyInput?.value.trim() ?? "";
  const pico = picovoiceKeyInput?.value.trim() ?? "";

  settingsSaveBtn.disabled = true;
  try {
    const existing = await bridge.loadCredentials?.() ?? {};
    const next = { ...existing };
    if (gemini) next.gemini_api_key = gemini;     // blank = keep what is stored
    if (pico) next.picovoice_key = pico;
    if (!next.user_id) {
      const id = await bridge.generateUserId?.() ?? {};
      Object.assign(next, id);
    }
    await bridge.saveCredentials?.(next);

    if (geminiKeyInput) geminiKeyInput.value = "";
    if (picovoiceKeyInput) picovoiceKeyInput.value = "";
    if (settingsSavedEl) {
      settingsSavedEl.textContent = "Saved — restart to apply";
      setTimeout(() => { settingsSavedEl.textContent = ""; }, 4000);
    }
  } catch {
    if (settingsSavedEl) settingsSavedEl.textContent = "Could not save";
  }
  settingsSaveBtn.disabled = false;
});

document.getElementById("onboarding-open-aistudio")?.addEventListener("click", (e) => {
  e.preventDefault();
  window.open("https://aistudio.google.com/app/apikey");
});
document.getElementById("onboarding-open-picovoice")?.addEventListener("click", (e) => {
  e.preventDefault();
  window.open("https://console.picovoice.ai");
});

// Esc from Settings returns to the welcome view.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (onboardingOverlay?.classList.contains("hidden")) return;
  if (settingsView?.classList.contains("active")) { showView("welcome"); e.stopPropagation(); }
});

// ── Chrome extension buttons (Settings) ──
const exportExtBtn = document.getElementById("onboarding-export-ext");
const revealExtBtn = document.getElementById("onboarding-reveal-ext");

if (exportExtBtn) {
  exportExtBtn.addEventListener("click", async () => {
    const label = exportExtBtn.textContent;
    exportExtBtn.textContent = "Saving…";
    exportExtBtn.disabled = true;
    try {
      const result = await bridge.exportExtension();
      if (result?.success) exportExtBtn.textContent = "Saved — opening folder…";
      else if (result?.reason === "cancelled") exportExtBtn.textContent = label;
      else exportExtBtn.textContent = "Failed — try again";
    } catch {
      exportExtBtn.textContent = "Error — try again";
    }
    exportExtBtn.disabled = false;
    setTimeout(() => { exportExtBtn.textContent = label; }, 3000);
  });
}

if (revealExtBtn) {
  revealExtBtn.addEventListener("click", () => { bridge.revealExtension(); });
}

setState(State.IDLE, { force: true });
wrapper.classList.remove("hidden");
setMouseEnabled(false);

// 1. Connect WS
connectWebSocket();

// 2. Start continuously recording and streaming Base64 WAV chunks
startAudioStreaming();

// 3. Run onboarding if first launch
runOnboarding();

/* ══════════════════════════════════════════════════════════════
   Foreground Agent Event Tracking (no drawer UI)
   ══════════════════════════════════════════════════════════════ */
// ── Utility Functions ──

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}


// ── Presentation mode ──
// main tells each window whether it is the active presentation. The inactive
// one drops its socket so the backend does not build a second agent for it.
if (window.overlayAPI && overlayAPI.onUiActive) {
  overlayAPI.onUiActive((active) => {
    app.uiInactive = !active;
    if (active) {
      app.reconnectDelay = 700;
      connectWebSocket();
    } else {
      try { app.ws && app.ws.close(); } catch (_) {}
      app.ws = null;
      if (app.reconnectTimer) {
        window.clearTimeout(app.reconnectTimer);
        app.reconnectTimer = null;
      }
    }
  });
}
