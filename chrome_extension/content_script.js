(function() {
// This IIFE prevents "Identifier has already been declared" errors 
// when the extension is injected multiple times into the same page.

if (window.__moonwalk_injected__) return;
window.__moonwalk_injected__ = true;

// ═══════════════════════════════════════════════════════════════
//  Moonwalk — Content Script v2
//  1. data-agent-id sequential tagging for instant element lookup
//  2. Aggressive DOM distillation (strips noise, pruned a11y tree)
//  3. MutationObserver-driven DOM change events for verify phase
// ═══════════════════════════════════════════════════════════════

const INTERACTIVE_SELECTOR = [
  "button",
  "a[href]",
  "input",
  "textarea",
  "select",
  "summary",
  "[contenteditable='true']",
  "[contenteditable='']",
  "[tabindex]:not([tabindex='-1'])",
  "img[alt]",
  "video",
  "[role='button']",
  "[role='link']",
  "[role='textbox']",
  "[role='searchbox']",
  "[role='combobox']",
  "[role='tab']",
  "[role='menuitem']",
  "[role='option']",
  "[role='radio']",
  "[role='checkbox']",
  "[role='switch']",
  "[role='slider']",
  "[role='treeitem']",
  "[role='gridcell']",
].join(",");

const READABLE_SELECTOR = [
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "p",
  "li",
  "label",
  "blockquote",
  "figcaption",
  "caption",
  "td",
  "th",
  "pre",
  "code",
].join(",");

// ── Noise tags to strip during DOM distillation ──
const NOISE_TAGS = new Set([
  "svg", "script", "style", "noscript", "link", "meta",
  "iframe", "object", "embed", "applet", "param", "source", "track",
]);

// ── Agent ID state ──
let _nextAgentId = 1;
const _agentIdMap = new Map(); // agent_id → Element (O(1) lookup)

// ═══════════════════════════════════════════════════════════════
//  Helpers
// ═══════════════════════════════════════════════════════════════

function textOf(node) {
  return (node?.innerText || node?.textContent || "").trim().replace(/\s+/g, " ").slice(0, 160);
}

function isVisible(el) {
  if (NOISE_TAGS.has(el.tagName.toLowerCase())) return false;
  const style = window.getComputedStyle(el);
  if (style.visibility === "hidden" || style.display === "none" || parseFloat(style.opacity) === 0) {
    return false;
  }
  if (el.getAttribute("aria-hidden") === "true") return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function isInViewport(el) {
  const rect = el.getBoundingClientRect();
  return (
    rect.bottom > 0 &&
    rect.right > 0 &&
    rect.top < window.innerHeight &&
    rect.left < window.innerWidth
  );
}

function roleOf(el) {
  return el.getAttribute("role") || el.tagName.toLowerCase();
}

function actionTypesFor(el) {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute("type") || "").toLowerCase();
  const role = roleOf(el).toLowerCase();
  const actions = new Set();

  if (
    ["button", "a", "summary"].includes(tag) ||
    ["button", "link", "tab", "menuitem", "switch", "treeitem", "gridcell"].includes(role)
  ) {
    actions.add("click");
  }
  if (
    ["input", "textarea"].includes(tag) ||
    ["textbox", "searchbox", "combobox"].includes(role) ||
    el.isContentEditable
  ) {
    actions.add("click");
    actions.add("type");
  }
  if (tag === "select" || ["combobox", "listbox", "option"].includes(role)) {
    actions.add("select");
  }
  if (tag === "input" && ["checkbox", "radio"].includes(type)) {
    actions.add("click");
  }
  if (tag === "input" && type === "range") {
    actions.add("click");
  }
  if (role === "slider") {
    actions.add("click");
  }
  if (tag === "img" || tag === "video") {
    actions.add("click");
  }
  if (actions.size === 0 && el.hasAttribute("tabindex")) {
    actions.add("click");
  }

  return [...actions];
}

function domPath(el) {
  const parts = [];
  let current = el;
  while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
    const tag = current.tagName.toLowerCase();
    const siblings = current.parentElement
      ? [...current.parentElement.children].filter((child) => child.tagName === current.tagName)
      : [current];
    const index = siblings.indexOf(current);
    parts.unshift(`${tag}:${index}`);
    current = current.parentElement;
  }
  return parts.join(">");
}

function ancestorLabels(el) {
  const labels = [];
  let current = el.parentElement;
  let depth = 0;
  while (current && labels.length < 3 && depth < 6) {
    depth++;
    const ariaLabel = (current.getAttribute("aria-label") || "").trim();
    if (ariaLabel) {
      labels.push(ariaLabel.slice(0, 80));
      current = current.parentElement;
      continue;
    }
    const innerText = textOf(current);
    if (innerText && innerText.length <= 120) {
      labels.push(innerText.slice(0, 80));
    }
    current = current.parentElement;
  }
  return labels;
}

// ═══════════════════════════════════════════════════════════════
//  1. data-agent-id Tagging
// ═══════════════════════════════════════════════════════════════

function assignAgentId(el) {
  const existing = el.getAttribute("data-agent-id");
  if (existing) {
    const id = parseInt(existing, 10);
    if (!isNaN(id) && id > 0) {
      _agentIdMap.set(id, el);
      _nextAgentId = Math.max(_nextAgentId, id + 1);
      return id;
    }
  }
  const id = _nextAgentId++;
  el.setAttribute("data-agent-id", String(id));
  _agentIdMap.set(id, el);
  return id;
}

function lookupByAgentId(agentId) {
  const fromMap = _agentIdMap.get(agentId);
  if (fromMap && fromMap.isConnected) return fromMap;
  const el = document.querySelector('[data-agent-id="' + agentId + '"]');
  if (el) _agentIdMap.set(agentId, el);
  return el;
}

function pruneAgentIdMap(maxEntries) {
  let scanned = 0;
  const limit = maxEntries || 2500;
  for (const [id, el] of _agentIdMap.entries()) {
    scanned++;
    if (!el || !el.isConnected) {
      _agentIdMap.delete(id);
    }
    if (scanned >= limit) break;
  }
}

function isReadableCandidate(el) {
  if (!isVisible(el)) return false;
  const text = textOf(el);
  if (!text) return false;

  const tag = el.tagName.toLowerCase();
  const headingLike = ["h1", "h2", "h3", "h4", "h5", "h6", "label", "th", "td"].includes(tag);
  if (headingLike) {
    return text.length >= 2;
  }

  if (text.length < 24) {
    return false;
  }

  // Avoid giant container-level blocks that duplicate the entire page.
  const childCount = el.children ? el.children.length : 0;
  if (childCount > 10 && text.length > 120) {
    return false;
  }

  return true;
}

// ═══════════════════════════════════════════════════════════════
//  Element Serialization (with agent-id)
// ═══════════════════════════════════════════════════════════════

function serializeElement(el, index) {
  const agentId = assignAgentId(el);
  const rect = el.getBoundingClientRect();
  const text = textOf(el);
  const ariaLabel = (el.getAttribute("aria-label") || "").trim();
  const name = (el.getAttribute("name") || "").trim();
  const placeholder = (el.getAttribute("placeholder") || "").trim();
  const href = (el.getAttribute("href") || "").trim();
  const alt = (el.getAttribute("alt") || "").trim();
  const title = (el.getAttribute("title") || "").trim();
  const role = roleOf(el);
  const path = domPath(el);
  const labels = ancestorLabels(el);
  const viewport = isInViewport(el);
  const refId = "mw_" + agentId;

  return {
    ref_id: refId,
    agent_id: agentId,
    role,
    tag: el.tagName.toLowerCase(),
    text: text || alt || title,
    aria_label: ariaLabel,
    name,
    placeholder,
    href,
    value: typeof el.value === "string" ? el.value.slice(0, 120) : "",
    context_text: labels.join(" | "),
    frame_path: "main",
    dom_path: path,
    bounds: {
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    },
    visible: true,
    enabled: !el.disabled,
    checked: !!el.checked,
    selected: !!el.selected,
    in_viewport: viewport,
    action_types: actionTypesFor(el),
    fingerprint: {
      role,
      text: text || alt || title,
      aria_label: ariaLabel,
      name,
      placeholder,
      href,
      ancestor_labels: labels,
      frame_path: "main",
      dom_path: path,
      sibling_index: index,
      stable_attributes: {
        id: el.id || "",
        class: (el.className || "").toString().slice(0, 120),
        type: el.getAttribute("type") || "",
      },
    },
  };
}

// ═══════════════════════════════════════════════════════════════
//  2. Element Collection (distilled + deduplicated)
// ═══════════════════════════════════════════════════════════════

function collectElements() {
  pruneAgentIdMap(3000);
  const seen = new Set();
  const results = [];
  const interactiveNodes = document.querySelectorAll(INTERACTIVE_SELECTOR);
  const readableNodes = document.querySelectorAll(READABLE_SELECTOR);

  for (const el of interactiveNodes) {
    if (results.length >= 320) break;
    if (seen.has(el)) continue;
    if (!isVisible(el)) continue;
    seen.add(el);
    results.push(serializeElement(el, results.length));
  }

  for (const el of readableNodes) {
    if (results.length >= 520) break;
    if (seen.has(el)) continue;
    if (!isReadableCandidate(el)) continue;
    seen.add(el);
    results.push(serializeElement(el, results.length));
  }

  return results;
}

function buildSnapshot(sessionId, tabId) {
  return {
    session_id: sessionId,
    tab_id: tabId,
    url: window.location.href,
    title: document.title,
    generation: Date.now(),
    frame_id: "main",
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      scrollX: Math.round(window.scrollX),
      scrollY: Math.round(window.scrollY),
      scrollHeight: document.documentElement.scrollHeight,
      pageHeight: document.documentElement.scrollHeight,
    },
    elements: collectElements(),
    opaque_regions: [],
  };
}

// ═══════════════════════════════════════════════════════════════
//  Action Execution (agent-id powered)
// ═══════════════════════════════════════════════════════════════

function normalizeText(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase();
}

/**
 * Resolve the target element for an action.
 * Priority: agent_id (O(1)) -> ref_id parse -> heuristic fallback.
 */
function findTargetForAction(action) {
  // -- Fast path: agent_id lookup (O(1)) --
  var agentId = action?.agent_id || action?.metadata?.agent_id;
  if (agentId) {
    var el = lookupByAgentId(Number(agentId));
    if (el && isVisible(el)) {
      return {
        element: el,
        payload: serializeElement(el, 0),
        score: 10000,
        matchedBy: "agent_id",
      };
    }
  }

  // -- Medium path: ref_id "mw_N" parse (O(1) via agent_id map) --
  var refId = action?.ref_id || "";
  if (refId.startsWith("mw_")) {
    var parsed = parseInt(refId.slice(3), 10);
    if (!isNaN(parsed)) {
      var el2 = lookupByAgentId(parsed);
      if (el2 && isVisible(el2)) {
        return {
          element: el2,
          payload: serializeElement(el2, 0),
          score: 10000,
          matchedBy: "ref_id",
        };
      }
    }
  }

  // -- Slow path: heuristic matching (fallback) --
  return findTargetByHeuristic(action);
}

function findTargetByHeuristic(action) {
  const candidates = collectActionCandidates();
  let best = null;

  for (const candidate of candidates) {
    const score = scoreCandidate(action, candidate.payload);
    if (score < 0) continue;
    if (!best || score > best.score) {
      best = { ...candidate, score, matchedBy: "heuristic" };
    }
  }

  if (!best || best.score <= 0) return null;
  return best;
}

function collectActionCandidates() {
  const seen = new Set();
  const results = [];
  const nodes = document.querySelectorAll(INTERACTIVE_SELECTOR);

  for (const el of nodes) {
    if (results.length >= 400) break;
    if (seen.has(el)) continue;
    if (!isVisible(el)) continue;
    seen.add(el);
    results.push({ element: el, payload: serializeElement(el, results.length) });
  }

  return results;
}

function scoreCandidate(action, payload) {
  const metadata = action?.metadata || {};
  const requestedAction = String(action?.action || "");
  if (
    requestedAction &&
    Array.isArray(payload.action_types) &&
    payload.action_types.length &&
    !payload.action_types.includes(requestedAction)
  ) {
    return -1;
  }

  let score = 0;
  if (payload.ref_id === action?.ref_id) score += 1000;
  if (metadata.dom_path && payload.dom_path === metadata.dom_path) score += 300;
  if (metadata.tag && payload.tag === metadata.tag) score += 40;
  if (metadata.role && payload.role === metadata.role) score += 40;

  const expectedLabels = [
    metadata.label,
    metadata.text,
    metadata.aria_label,
    metadata.name,
    metadata.placeholder,
    metadata.href,
  ]
    .map(normalizeText)
    .filter(Boolean);

  const actualLabels = [
    payload.text,
    payload.aria_label,
    payload.name,
    payload.placeholder,
    payload.href,
    payload.context_text,
  ]
    .map(normalizeText)
    .filter(Boolean);

  for (const expected of expectedLabels) {
    for (const actual of actualLabels) {
      if (actual === expected) score += 120;
      else if (actual.includes(expected) || expected.includes(actual)) score += 60;
    }
  }

  if (payload.in_viewport) score += 15;

  return score;
}

function dispatchInputEvents(el) {
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function setElementValue(el, value) {
  if (el instanceof HTMLInputElement) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter ? setter.call(el, value) : (el.value = value);
    return;
  }
  if (el instanceof HTMLTextAreaElement) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter ? setter.call(el, value) : (el.value = value);
    return;
  }
  if (el.isContentEditable) {
    el.textContent = value;
  }
}

function executeClick(el) {
  el.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
  el.focus?.({ preventScroll: true });
  if (typeof el.click === "function") {
    el.click();
    return;
  }
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
}

function executeType(el, action) {
  const text = String(action?.text || "");
  el.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
  el.focus?.({ preventScroll: true });

  // Contenteditable elements (Google Docs, Notion, rich text editors):
  // Use document.execCommand which goes through the browser editing pipeline.
  // Direct textContent/value assignment is silently ignored by canvas-based editors.
  if (el.isContentEditable) {
    if (action?.clear_first) {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
    }
    if (!document.execCommand("insertText", false, text)) {
      el.textContent = text;
    }
    dispatchInputEvents(el);
    return;
  }

  if (action?.clear_first) {
    setElementValue(el, "");
    dispatchInputEvents(el);
  }
  setElementValue(el, text);
  dispatchInputEvents(el);
}

function executeSelect(el, action) {
  if (!(el instanceof HTMLSelectElement)) {
    throw new Error("Target element is not a select control.");
  }
  const optionText = normalizeText(action?.option);
  const option = [...el.options].find(function(candidate) {
    return normalizeText(candidate.textContent) === optionText || normalizeText(candidate.value) === optionText;
  });
  if (!option) {
    throw new Error("Option not found: " + (action?.option || ""));
  }
  el.value = option.value;
  dispatchInputEvents(el);
}

async function executeAction(action, sessionId, tabId) {
  const match = findTargetForAction(action);
  if (!match) {
    return {
      ok: false,
      message: "Could not resolve target for " + (action?.ref_id || "unknown ref") + ".",
      executedRefId: "",
      matchedBy: "none",
    };
  }

  const { element, payload, matchedBy } = match;
  if (!payload.enabled) {
    return {
      ok: false,
      message: "Target " + payload.ref_id + " is disabled.",
      executedRefId: payload.ref_id,
      matchedBy: matchedBy || "unknown",
    };
  }

  try {
    if (action?.action === "click") {
      executeClick(element);
    } else if (action?.action === "type") {
      executeType(element, action);
    } else if (action?.action === "select") {
      executeSelect(element, action);
    } else {
      throw new Error("Unsupported action: " + (action?.action || "unknown"));
    }

    return {
      ok: true,
      message: action.action + " executed on " + payload.ref_id + " (via " + matchedBy + ")",
      executedRefId: payload.ref_id,
      matchedBy: matchedBy || "unknown",
    };
  } catch (error) {
    return {
      ok: false,
      message: String(error?.message || error),
      executedRefId: payload.ref_id,
      matchedBy: matchedBy || "unknown",
    };
  }
}

// -- Snapshot Sending (debounced) --

let _snapshotDebounceTimer = null;

async function sendSnapshot(sessionId, tabId) {
  const snapshot = buildSnapshot(sessionId, tabId);
  try {
    await chrome.runtime.sendMessage({
      type: "moonwalk_snapshot",
      snapshot,
    });
  } catch (error) {
    console.debug("[Moonwalk Content] Snapshot send failed", error);
  }
}

function debouncedSnapshot(sessionId, tabId, delayMs) {
  delayMs = delayMs || 300;
  if (_snapshotDebounceTimer) clearTimeout(_snapshotDebounceTimer);
  _snapshotDebounceTimer = setTimeout(function() {
    _snapshotDebounceTimer = null;
    sendSnapshot(sessionId, tabId);
  }, delayMs);
}

// ═══════════════════════════════════════════════════════════════
//  3. MutationObserver — Verify Phase + Auto-snapshot
// ═══════════════════════════════════════════════════════════════

let _observerActive = false;
let _observerSessionId = "";
let _observerTabId = "";
let _mutationBatch = 0;
const MUTATION_BATCH_THRESHOLD = 5;
const MUTATION_DEBOUNCE_MS = 400;

// Pending action verification (set before action execution)
let _pendingVerify = null;

function registerPendingVerify(actionId, refId, actionType) {
  _pendingVerify = {
    actionId: actionId,
    refId: refId,
    actionType: actionType,
    timestamp: Date.now(),
  };
}

function startMutationObserver(sessionId, tabId) {
  if (_observerActive) return;
  _observerActive = true;
  _observerSessionId = sessionId;
  _observerTabId = tabId;

  const observer = new MutationObserver(function(mutations) {
    let dominated = false;
    const changeTypes = new Set();

    for (const m of mutations) {
      if (m.type === "childList" && (m.addedNodes.length > 0 || m.removedNodes.length > 0)) {
        dominated = true;
        if (m.addedNodes.length > 0) changeTypes.add("nodes_added");
        if (m.removedNodes.length > 0) changeTypes.add("nodes_removed");
      }
      if (m.type === "attributes") {
        const attr = m.attributeName || "";
        // Ignore our own agent-id attribute changes
        if (attr === "data-agent-id") continue;
        if (["disabled", "aria-hidden", "hidden", "style", "class", "aria-expanded", "aria-selected", "checked"].includes(attr)) {
          dominated = true;
          changeTypes.add("attr_" + attr);
        }
      }
    }

    if (!dominated) return;

    // -- Verify phase: push dom_change_event if pending --
    if (_pendingVerify && (Date.now() - _pendingVerify.timestamp) < 10000) {
      try {
        chrome.runtime.sendMessage({
          type: "moonwalk_dom_change",
          event: {
            action_id: _pendingVerify.actionId,
            ref_id: _pendingVerify.refId,
            action_type: _pendingVerify.actionType,
            change_types: Array.from(changeTypes),
            timestamp: Date.now(),
            session_id: _observerSessionId,
            tab_id: _observerTabId,
          },
        });
      } catch (e) {
        console.debug("[Moonwalk Content] DOM change event send failed", e);
      }
      _pendingVerify = null; // one-shot per action
    }

    // -- Auto-snapshot on meaningful DOM changes --
    _mutationBatch++;
    if (_mutationBatch >= MUTATION_BATCH_THRESHOLD) {
      _mutationBatch = 0;
      debouncedSnapshot(_observerSessionId, _observerTabId, MUTATION_DEBOUNCE_MS);
    }
  });

  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["disabled", "aria-hidden", "hidden", "style", "class", "aria-expanded", "aria-selected", "checked", "data-agent-id"],
  });
}

// -- Message Handling --

// ── Moonwalk Agent Click Pointer ─────────────────────────────────
const CLICK_POINTER_ID = "mw-click-pointer";
const CLICK_POINTER_STYLE_ID = "mw-click-pointer-style";
let _clickPointerEl = null;
let _clickPointerBurstTimer = null;
let _clickPointerOutroTimer = null;

function ensureClickPointerStyles() {
  if (document.getElementById(CLICK_POINTER_STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = CLICK_POINTER_STYLE_ID;
  style.textContent = `
    @keyframes mw-ptr-intro {
      0%   { opacity: 0; transform: scale(0.3); }
      65%  { opacity: 1; transform: scale(1.18); }
      100% { opacity: 1; transform: scale(1); }
    }
    @keyframes mw-ptr-dwell {
      0%, 100% { transform: scale(1); }
      50%       { transform: scale(1.08); }
    }
    @keyframes mw-ptr-ring-expand {
      0%   { transform: scale(0.7); opacity: 0.9; }
      100% { transform: scale(2.2); opacity: 0; }
    }
    @keyframes mw-ptr-burst {
      0%   { transform: scale(0); opacity: 1; }
      60%  { transform: scale(2.6); opacity: 0.5; }
      100% { transform: scale(3.8); opacity: 0; }
    }
    @keyframes mw-ptr-outro {
      0%   { opacity: 1; transform: scale(1); }
      100% { opacity: 0; transform: scale(0.5); }
    }
    #${CLICK_POINTER_ID} {
      position: fixed;
      width: 0;
      height: 0;
      pointer-events: none;
      z-index: 2147483647;
      will-change: transform;
      /* translate is set dynamically via left/top */
    }
    #${CLICK_POINTER_ID} .mw-ptr-inner {
      position: absolute;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: rgba(0, 122, 255, 1);
      box-shadow:
        0 0 0 2px rgba(255,255,255,0.9),
        0 0 12px 0 rgba(0, 122, 255, 0.6);
      transform: translate(-50%, -50%) scale(0);
      transform-origin: center;
      animation: mw-ptr-intro 0.4s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
    }
    #${CLICK_POINTER_ID}.dwell .mw-ptr-inner {
      animation: mw-ptr-dwell 1.2s ease-in-out infinite;
      transform: translate(-50%, -50%) scale(1);
    }
    #${CLICK_POINTER_ID} .mw-ptr-ring {
      position: absolute;
      width: 32px;
      height: 32px;
      border-radius: 50%;
      border: 1.5px solid rgba(175, 82, 222, 0.55);
      transform: translate(-50%, -50%) scale(0.7);
      transform-origin: center;
      animation: mw-ptr-ring-expand 1.1s ease-out infinite;
    }
    #${CLICK_POINTER_ID} .mw-ptr-burst {
      position: absolute;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: rgba(0, 122, 255, 0.45);
      transform: translate(-50%, -50%) scale(0);
      transform-origin: center;
      opacity: 0;
      pointer-events: none;
    }
    #${CLICK_POINTER_ID}.burst .mw-ptr-burst {
      animation: mw-ptr-burst 0.5s cubic-bezier(0.23, 1, 0.32, 1) forwards;
    }
    #${CLICK_POINTER_ID}.outro {
      animation: mw-ptr-outro 0.4s ease-in forwards;
    }
  `;
  (document.head || document.documentElement).appendChild(style);
}

function showClickPointer(pageX, pageY) {
  ensureClickPointerStyles();

  // Convert page → viewport coordinates
  const vx = pageX - window.scrollX;
  const vy = pageY - window.scrollY;

  // Clean up any running outro
  if (_clickPointerOutroTimer) {
    clearTimeout(_clickPointerOutroTimer);
    _clickPointerOutroTimer = null;
  }

  if (_clickPointerEl) {
    // Move existing pointer smoothly
    _clickPointerEl.style.left = vx + "px";
    _clickPointerEl.style.top  = vy + "px";
    _clickPointerEl.classList.remove("outro", "burst");
    // Re-trigger dwell
    void _clickPointerEl.offsetWidth;
    _clickPointerEl.classList.add("dwell");
    return;
  }

  const ptr = document.createElement("div");
  ptr.id = CLICK_POINTER_ID;
  ptr.style.left = vx + "px";
  ptr.style.top  = vy + "px";
  ptr.innerHTML = `
    <div class="mw-ptr-ring"></div>
    <div class="mw-ptr-burst"></div>
    <div class="mw-ptr-inner"></div>
  `;
  (document.body || document.documentElement).appendChild(ptr);
  _clickPointerEl = ptr;

  // Switch to dwell animation after intro completes
  setTimeout(function() {
    if (_clickPointerEl) _clickPointerEl.classList.add("dwell");
  }, 420);
}

function triggerClickBurst() {
  if (!_clickPointerEl) return;
  _clickPointerEl.classList.remove("dwell");
  _clickPointerEl.classList.add("burst");

  // Start outro shortly after burst
  _clickPointerOutroTimer = setTimeout(function() {
    if (!_clickPointerEl) return;
    _clickPointerEl.classList.add("outro");
    _clickPointerOutroTimer = setTimeout(function() {
      if (_clickPointerEl) {
        _clickPointerEl.remove();
        _clickPointerEl = null;
      }
    }, 420);
  }, 350);
}

// ── Moonwalk Research Highlight Styles ──
const HIGHLIGHT_STYLE_ID = "moonwalk-research-highlight-style";
const RESEARCH_OVERLAY_ID = "moonwalk-research-overlay";
let _researchOverlayTimer = null;


function ensureHighlightStyles() {
  if (document.getElementById(HIGHLIGHT_STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = HIGHLIGHT_STYLE_ID;
  style.textContent = `
    /* ── Houdini custom property for animated conic angle (Chrome 85+) ── */
    @property --mw-angle {
      syntax: '<angle>';
      initial-value: 0deg;
      inherits: false;
    }

    /* ── Keyframes ── */
    @keyframes mw-rotate-gradient {
      to { --mw-angle: 360deg; }
    }
    @keyframes mw-gradient-flow {
      0%   { background-position: 0% 50%; }
      100% { background-position: 300% 50%; }
    }
    @keyframes mw-sweep {
      0%   { left: -150%; }
      100% { left: 200%; }
    }
    @keyframes mw-intro {
      0%   { opacity: 0; transform: scaleY(0.6) scaleX(0.97); }
      60%  { opacity: 1; transform: scaleY(1.04) scaleX(1); }
      100% { opacity: 1; transform: scaleY(1) scaleX(1); }
    }
    @keyframes mw-outro {
      0%   { opacity: 1; transform: scale(1); }
      100% { opacity: 0; transform: scaleY(0.7) scaleX(0.98); }
    }
    @keyframes mw-glow-pulse {
      0%, 100% {
        box-shadow:
          0 0 0 0 transparent,
          0 8px 24px -4px rgba(0, 122, 255, 0.12);
      }
      50% {
        box-shadow:
          0 0 20px 2px rgba(175, 82, 222, 0.15),
          0 12px 32px -4px rgba(0, 122, 255, 0.18);
      }
    }

    /* ── Shared base ── */
    .mw-hl, .mw-hl-text {
      position: relative !important;
      isolation: isolate !important;
    }

    /* ===========================================================
       ELEMENT / DIV HIGHLIGHT — Spinning conic-gradient border
       =========================================================== */
    .mw-hl::before {
      content: '' !important;
      position: absolute !important;
      inset: -3px !important;
      border-radius: 12px !important;
      pointer-events: none !important;
      z-index: 2147483640 !important;
      transform-origin: center center !important;
      opacity: 0 !important;
      background: conic-gradient(
        from var(--mw-angle),
        transparent 0%,
        transparent 55%,
        #af52de 75%,
        #007aff 95%,
        transparent 100%
      ) !important;
      /* Mask trick: gradient only visible in the 2px ring (padding area) */
      -webkit-mask:
        linear-gradient(#fff 0 0) content-box,
        linear-gradient(#fff 0 0) !important;
      -webkit-mask-composite: xor !important;
      mask-composite: exclude !important;
      padding: 2px !important;
    }

    /* Phase: intro */
    .mw-hl--intro::before {
      animation: mw-intro 0.45s cubic-bezier(0.34, 1.56, 0.64, 1) forwards !important;
    }
    .mw-hl--intro {
      transition: transform 0.4s cubic-bezier(0.2, 0.8, 0.2, 1),
                  box-shadow 0.4s ease !important;
    }

    /* Phase: reading — spinning border + lift + glow */
    .mw-hl--reading::before {
      opacity: 1 !important;
      animation: mw-rotate-gradient 3s linear infinite !important;
    }
    .mw-hl--reading {
      transform: translateY(-3px) !important;
      animation: mw-glow-pulse 3s ease-in-out infinite !important;
      transition: transform 0.4s cubic-bezier(0.2, 0.8, 0.2, 1) !important;
    }

    /* Phase: outro */
    .mw-hl--outro::before {
      animation: mw-outro 0.5s ease-in forwards !important;
    }
    .mw-hl--outro {
      transform: translateY(0) !important;
      box-shadow: none !important;
      transition: transform 0.5s ease, box-shadow 0.5s ease !important;
    }

    /* ===========================================================
       TEXT HIGHLIGHT — Fluid gradient behind text
       =========================================================== */
    .mw-hl-text::before {
      content: '' !important;
      position: absolute !important;
      inset: -2px -5px !important;
      border-radius: 6px !important;
      pointer-events: none !important;
      z-index: -1 !important;
      transform-origin: center center !important;
      opacity: 0 !important;
      background: linear-gradient(
        90deg, #007aff, #af52de, #ff2d55, #007aff
      ) !important;
      background-size: 300% 100% !important;
    }

    /* Phase: intro */
    .mw-hl-text--intro::before {
      animation: mw-intro 0.45s cubic-bezier(0.34, 1.56, 0.64, 1) forwards !important;
    }

    /* Phase: reading — flowing gradient + white text */
    .mw-hl-text--reading::before {
      opacity: 1 !important;
      animation: mw-gradient-flow 3s linear infinite !important;
    }
    .mw-hl-text--reading {
      color: #ffffff !important;
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.15) !important;
      transition: color 0.4s ease !important;
    }

    /* Phase: outro */
    .mw-hl-text--outro::before {
      animation: mw-outro 0.5s ease-in forwards !important;
    }
    .mw-hl-text--outro {
      color: inherit !important;
      text-shadow: none !important;
    }

    /* ===========================================================
       MEDIA — Glass sweep + subtle scale
       =========================================================== */
    .mw-hl--reading img,
    .mw-hl--reading video {
      transform: scale(1.03) !important;
      transition: transform 2s ease !important;
    }
    .mw-hl--outro img,
    .mw-hl--outro video {
      transform: scale(1) !important;
    }

    .mw-media-sweep {
      position: absolute !important;
      top: 0 !important;
      left: -150% !important;
      width: 50% !important;
      height: 100% !important;
      background: linear-gradient(
        to right,
        rgba(255,255,255,0),
        rgba(255,255,255,0.55),
        rgba(255,255,255,0)
      ) !important;
      transform: skewX(-25deg) !important;
      z-index: 2147483641 !important;
      pointer-events: none !important;
      animation: mw-sweep 2.5s cubic-bezier(0.4, 0, 0.2, 1) infinite !important;
    }

    /* ===========================================================
       PAGE-WIDE SCANNING MODE — Edge borders + progress beam
       =========================================================== */
    @keyframes mw-scan-beam {
      0%   { top: -6px; opacity: 0; }
      5%   { opacity: 1; }
      95%  { opacity: 1; }
      100% { top: 100%; opacity: 0; }
    }
    @keyframes mw-scan-border-pulse {
      0%, 100% { opacity: 0.35; }
      50%      { opacity: 0.7; }
    }

    .mw-scanning-frame {
      position: fixed !important;
      inset: 0 !important;
      z-index: 2147483646 !important;
      pointer-events: none !important;
      border: 2px solid transparent !important;
      border-image: linear-gradient(
        135deg, #007aff, #af52de, #ff2d55, #007aff
      ) 1 !important;
      animation: mw-scan-border-pulse 2s ease-in-out infinite !important;
      transition: opacity 0.5s ease !important;
    }

    .mw-scanning-beam {
      position: fixed !important;
      left: 0 !important;
      right: 0 !important;
      height: 3px !important;
      z-index: 2147483647 !important;
      pointer-events: none !important;
      background: linear-gradient(
        90deg,
        transparent 0%,
        #007aff 15%,
        #af52de 50%,
        #007aff 85%,
        transparent 100%
      ) !important;
      box-shadow: 0 0 12px 3px rgba(0, 122, 255, 0.4),
                  0 0 30px 6px rgba(175, 82, 222, 0.15) !important;
      animation: mw-scan-beam var(--mw-scan-duration, 3s)
                 cubic-bezier(0.22, 0.61, 0.36, 1) forwards !important;
    }

    .mw-scanning-label {
      position: fixed !important;
      top: 10px !important;
      right: 14px !important;
      z-index: 2147483647 !important;
      pointer-events: none !important;
      font: 600 11px/1.2 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      color: rgba(255, 255, 255, 0.95) !important;
      background: linear-gradient(135deg, rgba(0,122,255,0.7), rgba(175,82,222,0.7)) !important;
      backdrop-filter: blur(16px) saturate(1.5) !important;
      -webkit-backdrop-filter: blur(16px) saturate(1.5) !important;
      padding: 5px 12px 5px 10px !important;
      border-radius: 14px !important;
      letter-spacing: 0.3px !important;
      box-shadow: 0 2px 12px rgba(0,0,0,0.15) !important;
      opacity: 0 !important;
      transform: translateY(-8px) !important;
      animation: mw-intro 0.35s 0.1s cubic-bezier(0.34, 1.56, 0.64, 1) forwards !important;
    }
  `;
  (document.head || document.documentElement).appendChild(style);
}

function ensureResearchOverlay() {
  // No overlay — the highlight itself is sufficient feedback
  ensureHighlightStyles();
  return null;
}

function overlayFieldValue(overlay, field) {
  return null;
}

function setOverlayField(overlay, field, value, fallback) {
  // no-op
}

function hideResearchOverlay() {
  // no-op — overlay removed
}

function showResearchOverlay(details, durationMs) {
  // no-op — overlay removed
}

// Apply the 3-phase highlight lifecycle to a single element with a stagger offset
function _applyHighlightLifecycle(el, baseCls, staggerMs, durationMs) {
  const introPhase   = baseCls + "--intro";
  const readingPhase = baseCls + "--reading";
  const outroPhase   = baseCls + "--outro";
  const outroDuration = 500; // ms — matches mw-outro animation
  const introToReading = 450; // ms — matches mw-intro duration

  // Detect media elements for glass sweep injection
  var mediaEls = [];
  var tag = el.tagName.toLowerCase();
  if (tag === "img" || tag === "video") {
    mediaEls.push(el);
  } else {
    var innerMedia = el.querySelectorAll("img, video");
    for (var i = 0; i < innerMedia.length; i++) {
      if (isVisible(innerMedia[i])) mediaEls.push(innerMedia[i]);
    }
  }

  // 1. Add base + intro (staggered)
  setTimeout(function() {
    el.classList.add(baseCls, introPhase);

    // Inject glass sweep overlays on media elements
    var sweepOverlays = [];
    for (var m = 0; m < mediaEls.length; m++) {
      var media = mediaEls[m];
      var parent = media.parentElement;
      if (!parent) continue;
      var pPos = window.getComputedStyle(parent).position;
      if (pPos === "static") parent.style.position = "relative";
      var hadOverflow = parent.style.overflow;
      parent.style.overflow = "hidden";
      var sweep = document.createElement("div");
      sweep.className = "mw-media-sweep";
      parent.appendChild(sweep);
      sweepOverlays.push({ sweep: sweep, parent: parent, hadPosition: pPos, hadOverflow: hadOverflow });
    }

    // 2. Transition to reading phase after intro completes
    setTimeout(function() {
      el.classList.remove(introPhase);
      el.classList.add(readingPhase);
    }, introToReading);

    // 3. Trigger outro phase before duration ends
    var totalAfterStart = Math.max(durationMs - staggerMs - outroDuration, introToReading + 200);
    setTimeout(function() {
      el.classList.remove(readingPhase);
      el.classList.add(outroPhase);

      // 4. Clean up after outro animation completes
      setTimeout(function() {
        el.classList.remove(baseCls, outroPhase);
        // Remove sweep overlays and restore parent styles
        for (var s = 0; s < sweepOverlays.length; s++) {
          sweepOverlays[s].sweep.remove();
          if (sweepOverlays[s].hadPosition === "static") {
            sweepOverlays[s].parent.style.position = "";
          }
          sweepOverlays[s].parent.style.overflow = sweepOverlays[s].hadOverflow || "";
        }
      }, outroDuration + 50);
    }, totalAfterStart);
  }, staggerMs);
}

function highlightElements(agentIds, durationMs, mode, overlayDetails) {
  ensureHighlightStyles();
  durationMs = durationMs || 3000;
  mode = mode || "reading";
  const baseCls = mode === "text" ? "mw-hl-text" : "mw-hl";
  const highlighted = [];

  for (const aid of agentIds) {
    const el = lookupByAgentId(Number(aid));
    if (!el) continue;
    const stagger = highlighted.length * 80;
    _applyHighlightLifecycle(el, baseCls, stagger, durationMs);
    highlighted.push(el);
    if (highlighted.length === 1) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  return highlighted.length;
}

function highlightReadableContent(durationMs, overlayDetails) {
  ensureHighlightStyles();
  durationMs = durationMs || 4000;
  const readableNodes = document.querySelectorAll(READABLE_SELECTOR);
  const highlighted = [];

  for (const el of readableNodes) {
    if (!isVisible(el) || !isInViewport(el)) continue;
    if (!isReadableCandidate(el)) continue;
    const stagger = highlighted.length * 60;
    _applyHighlightLifecycle(el, "mw-hl-text", stagger, durationMs);
    highlighted.push(el);
    if (highlighted.length >= 30) break;
  }

  return highlighted.length;
}


// ── Page-wide AI scanning mode ──
let _scanningCleanup = null;

function startScanningMode(labelText, durationMs) {
  stopScanningMode(); // clean up any previous
  ensureHighlightStyles();
  durationMs = Math.max(1500, Math.min(durationMs || 4000, 15000));

  // 1. Edge frame
  const frame = document.createElement("div");
  frame.className = "mw-scanning-frame";
  document.body.appendChild(frame);

  // 2. Scan beam
  const beam = document.createElement("div");
  beam.className = "mw-scanning-beam";
  beam.style.setProperty("--mw-scan-duration", durationMs + "ms");
  document.body.appendChild(beam);

  // 3. Label pill
  const label = document.createElement("div");
  label.className = "mw-scanning-label";
  label.textContent = labelText || "AI analyzing page…";
  document.body.appendChild(label);

  // 4. Also cascade-highlight visible readable content
  const readableNodes = document.querySelectorAll(READABLE_SELECTOR);
  const highlighted = [];
  for (const el of readableNodes) {
    if (!isVisible(el) || !isInViewport(el)) continue;
    if (!isReadableCandidate(el)) continue;
    const stagger = highlighted.length * 80;
    _applyHighlightLifecycle(el, "mw-hl-text", stagger, durationMs);
    highlighted.push(el);
    if (highlighted.length >= 20) break;
  }

  // Store cleanup
  const cleanupTimer = setTimeout(function() {
    stopScanningMode();
  }, durationMs + 600);

  _scanningCleanup = function() {
    clearTimeout(cleanupTimer);
    // Fade out frame
    frame.style.opacity = "0";
    beam.style.opacity = "0";
    label.style.opacity = "0";
    label.style.transform = "translateY(-8px)";
    setTimeout(function() {
      frame.remove();
      beam.remove();
      label.remove();
    }, 500);
    _scanningCleanup = null;
  };

  return highlighted.length;
}

function stopScanningMode() {
  if (_scanningCleanup) {
    _scanningCleanup();
    _scanningCleanup = null;
  }
  // Also clean up any leftover elements
  document.querySelectorAll(".mw-scanning-frame, .mw-scanning-beam, .mw-scanning-label")
    .forEach(function(el) { el.remove(); });
}

const READABILITY_MIN_TEXT_CHARS = 200;
const READABILITY_MAX_TEXT_CHARS = 12000;

function normalizeReadabilityText(value) {
  const normalized = String(value || "")
    .replace(/\u00a0/g, " ")
    .replace(/\r/g, "\n")
    .split(/\n+/)
    .map(function(line) {
      return line.trim().replace(/\s+/g, " ");
    })
    .filter(Boolean)
    .join("\n\n")
    .trim();
  return normalized;
}

function extractReadabilityArticle() {
  if (typeof Readability !== "function") {
    return {
      ok: false,
      message: "Readability.js is not available in the content script.",
      error: "missing_readability",
    };
  }

  try {
    const clonedDocument = document.cloneNode(true);
    const article = new Readability(clonedDocument).parse();
    if (!article) {
      return {
        ok: false,
        message: "Readability could not parse the current page.",
        error: "parse_failed",
      };
    }

    const cleanText = normalizeReadabilityText(article.textContent || "");
    const rawLength = cleanText.length;
    if (rawLength < READABILITY_MIN_TEXT_CHARS) {
      return {
        ok: false,
        message: "Readability returned too little readable text.",
        error: "thin_content",
        title: String(article.title || "").trim(),
        excerpt: normalizeReadabilityText(article.excerpt || ""),
        byline: normalizeReadabilityText(article.byline || "").slice(0, 240),
        site_name: normalizeReadabilityText(article.siteName || "").slice(0, 160),
        lang: String(document.documentElement?.lang || "").trim(),
        text: cleanText,
        content_length: rawLength,
      };
    }

    const text = cleanText.slice(0, READABILITY_MAX_TEXT_CHARS);
    return {
      ok: true,
      message: "Readability extracted readable page text.",
      title: String(article.title || "").trim(),
      excerpt: normalizeReadabilityText(article.excerpt || ""),
      byline: normalizeReadabilityText(article.byline || "").slice(0, 240),
      site_name: normalizeReadabilityText(article.siteName || "").slice(0, 160),
      lang: String(document.documentElement?.lang || "").trim(),
      text: text,
      content_length: rawLength,
      truncated: rawLength > text.length,
    };
  } catch (error) {
    return {
      ok: false,
      message: "Readability extraction threw an error.",
      error: String(error?.message || error),
    };
  }
}

chrome.runtime.onMessage.addListener(function(message, sender, sendResponse) {
  if (message?.type === "moonwalk_collect_snapshot") {
    const sid = message.sessionId || _observerSessionId;
    const tid = message.tabId || _observerTabId;
    _observerSessionId = sid;
    _observerTabId = tid;
    sendSnapshot(sid, tid);
    startMutationObserver(sid, tid);
    sendResponse?.({ ok: true });
    return true;
  }
  if (message?.type === "moonwalk_scroll") {
    var direction = message.direction || "down";
    var amount = message.amount || "page";
    var pixels = 0;
    var viewH = window.innerHeight;

    if (amount === "page") pixels = Math.round(viewH * 0.85);
    else if (amount === "half") pixels = Math.round(viewH * 0.5);
    else pixels = parseInt(amount, 10) || Math.round(viewH * 0.85);

    if (direction === "up") pixels = -pixels;
    else if (direction === "top") { window.scrollTo({ top: 0, behavior: "auto" }); pixels = 0; }
    else if (direction === "bottom") { window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" }); pixels = 0; }

    if (pixels !== 0) window.scrollBy({ top: pixels, behavior: "auto" });

    // Brief pause for scroll to settle, then send fresh snapshot + result
    setTimeout(function() {
      var sid = message.sessionId || _observerSessionId;
      var tid = message.tabId || _observerTabId;
      sendSnapshot(sid, tid);
      sendResponse?.({
        ok: true,
        scrollY: Math.round(window.scrollY),
        pageHeight: document.documentElement.scrollHeight,
        viewportHeight: window.innerHeight,
        atBottom: (window.scrollY + window.innerHeight) >= (document.documentElement.scrollHeight - 5),
        atTop: window.scrollY <= 0,
      });
    }, 120);
    return true;
  }
  if (message?.type === "moonwalk_evaluate_js") {
    try {
      const result = window.eval(message.script);
      sendResponse?.({ ok: true, result: String(result) });
    } catch (error) {
      sendResponse?.({ ok: false, error: String(error?.message || error) });
    }
    return true;
  }
  
  if (message?.type === "moonwalk_extract_data") {
    try {
      const target = message.target;
      let result = "";
      if (target === "gdocs") {
         const kix = document.querySelector('.kix-appview-editor');
         if (kix && kix.innerText) result = kix.innerText.substring(0, 8000);
         else if (document.body && document.body.innerText) result = document.body.innerText.substring(0, 8000);
         else if (document.documentElement && document.documentElement.innerText) result = document.documentElement.innerText.substring(0, 8000);
      } else if (target === "gdocs_state") {
         const titleInput =
           document.querySelector('input.docs-title-input') ||
           document.querySelector('input[aria-label="Document title"]') ||
           document.querySelector('input[aria-label="Rename"]') ||
           document.querySelector('input[placeholder="Untitled document"]');
         const editor =
           document.querySelector('.kix-appview-editor') ||
           document.querySelector('[contenteditable="true"]') ||
           document.querySelector('.docs-texteventtarget-iframe');
         const editorText = editor && typeof editor.innerText === "string" ? editor.innerText.trim() : "";
         result = JSON.stringify({
           url: location.href,
           title_value: titleInput ? (titleInput.value || '') : '',
           title_visible: !!titleInput,
           editor_ready: !!editor,
           body_length: editorText.length,
         });
      } else if (typeof target === "string" && target.startsWith("gdocs_set_title:")) {
         const titleInput =
           document.querySelector('input.docs-title-input') ||
           document.querySelector('input[aria-label="Document title"]') ||
           document.querySelector('input[aria-label="Rename"]') ||
           document.querySelector('input[placeholder="Untitled document"]');
         if (!titleInput) {
           result = "no-title-input";
         } else {
           const encoded = target.slice("gdocs_set_title:".length);
           const title = decodeURIComponent(escape(atob(encoded)));
           const valueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
           titleInput.focus();
           if (valueSetter) valueSetter.call(titleInput, title);
           else titleInput.value = title;
           titleInput.dispatchEvent(new Event("input", { bubbles: true }));
           titleInput.dispatchEvent(new Event("change", { bubbles: true }));
           titleInput.blur();
           result = titleInput.value || "";
         }
      } else if (target === "gdocs_focus_editor") {
         const editor =
           document.querySelector('textarea.kix-clipboard-capture-area') ||
           document.querySelector('.docs-texteventtarget-iframe') ||
           document.querySelector('.kix-appview-editor') ||
           document.querySelector('[contenteditable="true"]');
         if (!editor) {
           result = "no-editor";
         } else {
           if (editor.contentWindow && editor.contentWindow.focus) editor.contentWindow.focus();
           if (editor.focus) editor.focus();
           if (editor.click) editor.click();
           const active = document.activeElement;
           result = active ? (active.tagName || "ok") : "ok";
         }
      } else if (target === "gdocs_click_editor") {
         const clickTarget =
           document.querySelector('.kix-page-paginated') ||
           document.querySelector('.kix-page') ||
           document.querySelector('.kix-appview-editor') ||
           document.querySelector('.docs-texteventtarget-iframe') ||
           document.querySelector('textarea.kix-clipboard-capture-area');
         if (!clickTarget || !clickTarget.getBoundingClientRect) {
           result = "no-editor";
         } else {
           const rect = clickTarget.getBoundingClientRect();
           const clientX = rect.left + Math.max(24, Math.min(rect.width / 2, rect.width - 24));
           const clientY = rect.top + Math.max(24, Math.min(rect.height / 2, rect.height - 24));
           const node = document.elementFromPoint(clientX, clientY) || clickTarget;
           ["mousemove", "mousedown", "mouseup", "click"].forEach((type) => {
             node.dispatchEvent(new MouseEvent(type, {
               bubbles: true,
               cancelable: true,
               view: window,
               clientX,
               clientY,
               button: 0,
             }));
           });
           const active = document.activeElement;
           result = active ? (active.tagName || "ok") : "ok";
         }
      } else if (target === "gdocs_read_body") {
         const selectors = [
           '.kix-wordhtmlgenerator-word-node',
           '.kix-lineview-text-block',
           '.kix-paragraphrenderer',
           '[role="textbox"]',
           '.kix-appview-editor',
         ];
         const parts = [];
         const seen = new Set();
         for (const selector of selectors) {
           const nodes = document.querySelectorAll(selector);
           for (const node of nodes) {
             const text = (node && node.innerText ? node.innerText : "").trim();
             if (!text || text.length < 2 || seen.has(text)) continue;
             if (/^File Edit View Insert/i.test(text)) continue;
             seen.add(text);
             parts.push(text);
             if (parts.join("\n").length > 12000) break;
           }
           if (parts.join("\n").length > 12000) break;
         }
         result = parts.join("\n").slice(0, 12000);
      } else if (target === "gcal") {
         var chips = document.querySelectorAll('[data-eventid],[data-eventchip-action],.KF4T6b,.lKHqkb');
         var evs = [];
         var max_results = 250;
         for(var i=0; i < Math.min(chips.length, max_results); i++){
           var c = chips[i];
           evs.push({title: c.getAttribute('data-tooltip') || c.getAttribute('aria-label') || c.innerText.slice(0,80)});
         }
         result = JSON.stringify(evs);
      } else if (target === "body") {
         var t = document.body ? document.body.innerText : "";
         result = t ? t.substring(0, 8000) : "";
      }
      sendResponse?.({ ok: true, result: String(result) });
    } catch (error) {
      sendResponse?.({ ok: false, error: String(error?.message || error) });
    }
    return true;
  }
  if (message?.type === "moonwalk_extract_readability") {
    sendResponse?.(extractReadabilityArticle());
    return true;
  }
  if (message?.type === "moonwalk_execute_action") {
    const action = message.action;
    // Register pending verify BEFORE executing so MutationObserver
    // catches changes triggered by this action
    if (action?.action_id) {
      registerPendingVerify(action.action_id, action.ref_id, action.action);
    }
    executeAction(action, message.sessionId, message.tabId)
      .then(function(result) { sendResponse?.(result); })
      .catch(function(error) {
        sendResponse?.({
          ok: false,
          message: String(error?.message || error),
          executedRefId: "",
          matchedBy: "none",
        });
      });
    return true;
  }
  // ── Highlight elements the agent is reading/researching ──
  if (message?.type === "moonwalk_highlight") {
    const agentIds = message.agentIds || [];
    const duration = message.duration || 3000;
    const mode = message.mode || "reading";
    const overlayDetails = {
      tool: message.tool || "",
      title: message.title || "",
      sourceUrl: message.sourceUrl || "",
      snippet: message.snippet || "",
      itemCount: Number(message.itemCount || 0),
    };
    let count = 0;
    if (agentIds.length > 0) {
      count = highlightElements(agentIds, duration, mode, overlayDetails);
    } else {
      // No specific IDs → highlight all visible readable content
      count = highlightReadableContent(duration, overlayDetails);
    }
    sendResponse?.({ ok: true, highlighted: count, overlayVisible: true });
    return true;
  }
  // ── AI scanning mode — persistent page-wide visual while AI reads ──
  if (message?.type === "moonwalk_scanning_start") {
    const label = message.label || "AI analyzing page…";
    const duration = Number(message.duration || 4000);
    const count = startScanningMode(label, duration);
    sendResponse?.({ ok: true, highlighted: count });
    return true;
  }
  if (message?.type === "moonwalk_scanning_stop") {
    stopScanningMode();
    sendResponse?.({ ok: true });
    return true;
  }
  // ── Agent click pointer ──
  if (message?.type === "moonwalk_show_click_pointer") {
    const pageX = Number(message.pageX || 0);
    const pageY = Number(message.pageY || 0);
    if (pageX || pageY) showClickPointer(pageX, pageY);
    sendResponse?.({ ok: true });
    return true;
  }
  if (message?.type === "moonwalk_trigger_click_burst") {
    triggerClickBurst();
    sendResponse?.({ ok: true });
    return true;
  }
  return false;
});
})();
