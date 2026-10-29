// ClarityOS Browser Bridge popup.
//
// The previous version awaited chrome.runtime.sendMessage with no try/catch and
// no timeout. Under MV3 the background service worker is terminated after ~30s
// idle, so the first message from a freshly opened popup can reject with
// "Could not establish connection. Receiving end does not exist." — that
// rejection was unhandled, renderStatus() never ran, and the markup's initial
// "Loading…" text stayed on screen forever. Every status read now resolves to a
// rendered state or a visible error.

const STATUS_TIMEOUT_MS = 5000;
const SNAPSHOT_TIMEOUT_MS = 15000;
// A cold service worker has to be started by Chrome, parse its module, read
// chrome.storage and open its socket before it can answer. A single 4s deadline
// turned that normal cold start into a dead-end "Unavailable", so retry briefly
// before declaring the worker unresponsive.
const WAKE_ATTEMPTS = 3;
const WAKE_RETRY_DELAY_MS = 600;

// WebSocket.readyState is not available in a popup window that never opens a
// socket of its own, so mirror the constants explicitly.
const SOCKET_LABELS = {
  0: "Connecting",
  1: "Open",
  2: "Closing",
  3: "Closed",
};

const el = (id) => document.getElementById(id);

const dom = {
  badge: el("statusBadge"),
  badgeLabel: el("statusLabel"),
  socketValue: el("socketValue"),
  sessionValue: el("sessionValue"),
  snapshotCount: el("snapshotCount"),
  elementCount: el("elementCount"),
  activityNote: el("activityNote"),
  snapshotBody: el("snapshotBody"),
  snapshotEmpty: el("snapshotEmpty"),
  tabTitle: el("tabTitle"),
  tabUrl: el("tabUrl"),
  tabCaptured: el("tabCaptured"),
  tabElements: el("tabElements"),
  pairBtn: el("pairBtn"),
  refreshBtn: el("refreshBtn"),
  snapshotBtn: el("snapshotBtn"),
  feedback: el("feedback"),
  diagUrl: el("diagUrl"),
  diagExt: el("diagExt"),
  diagSocket: el("diagSocket"),
  diagAuth: el("diagAuth"),
  diagSession: el("diagSession"),
  diagTab: el("diagTab"),
  diagGen: el("diagGen"),
  diagError: el("diagError"),
};

let inFlight = false;

// ── Helpers ──

function middleTruncate(value, head = 10, tail = 6) {
  const text = String(value ?? "");
  if (text.length <= head + tail + 1) return text;
  return `${text.slice(0, head)}…${text.slice(-tail)}`;
}

function relativeTime(ms) {
  const stamp = Number(ms);
  if (!Number.isFinite(stamp) || stamp <= 0) return "unknown time";
  const delta = Date.now() - stamp;
  if (delta < 0 || delta < 1500) return "just now";
  if (delta < 60000) return `${Math.round(delta / 1000)}s ago`;
  if (delta < 3600000) return `${Math.round(delta / 60000)}m ago`;
  return new Date(stamp).toLocaleTimeString();
}

function setText(node, text) {
  node.textContent = text === undefined || text === null || text === "" ? "—" : String(text);
}

function setValueTone(node, tone) {
  node.classList.remove("ok", "warn", "bad");
  if (tone) node.classList.add(tone);
}

function setBadge(state, label) {
  dom.badge.className = `badge is-${state}`;
  dom.badgeLabel.textContent = label;
}

function setFeedback(text, tone) {
  dom.feedback.className = "feedback" + (tone ? ` is-${tone}` : "");
  dom.feedback.textContent = text || "";
}

function setBusy(busy, activeButton) {
  inFlight = busy;
  dom.refreshBtn.disabled = busy;
  dom.snapshotBtn.disabled = busy;
  if (busy && activeButton) {
    activeButton.dataset.idleLabel = activeButton.dataset.idleLabel || activeButton.textContent;
    activeButton.textContent = activeButton === dom.snapshotBtn ? "Capturing…" : "Refreshing…";
  } else {
    for (const button of [dom.refreshBtn, dom.snapshotBtn]) {
      if (button.dataset.idleLabel) button.textContent = button.dataset.idleLabel;
    }
  }
}

// Reject rather than hang: a service worker that wakes but stalls would
// otherwise leave the popup in its pending state indefinitely.
function sendMessage(payload, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`The background service worker did not respond within ${Math.round(timeoutMs / 1000)}s.`)),
      timeoutMs,
    );
    let settled = false;
    const finish = (fn) => (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(value);
    };
    try {
      chrome.runtime
        .sendMessage(payload)
        .then(finish(resolve), finish(reject));
    } catch (error) {
      finish(reject)(error instanceof Error ? error : new Error(String(error)));
    }
  });
}

// Sending any message is what starts a terminated worker, so a timeout on the
// first try means "still booting" far more often than "broken". Only a worker
// that misses every attempt is actually unresponsive.
async function sendWithWake(payload, timeoutMs) {
  let lastError = null;
  for (let attempt = 1; attempt <= WAKE_ATTEMPTS; attempt += 1) {
    try {
      return await sendMessage(payload, timeoutMs);
    } catch (error) {
      lastError = error;
      if (attempt >= WAKE_ATTEMPTS) break;
      setBadge("loading", `Waking worker… (${attempt + 1}/${WAKE_ATTEMPTS})`);
      await new Promise((resolve) => setTimeout(resolve, WAKE_RETRY_DELAY_MS));
    }
  }
  throw lastError;
}

// ── Rendering ──

// Connection health is derived from the socket + handshake only. A stored
// snapshot says nothing about whether the persistent WebSocket is open right
// now, so the two are rendered independently.
function describeConnection(status) {
  const socketState = Number(status.socketState);
  const hasError = Boolean(status.lastBridgeError);

  if (socketState === 1 && status.authenticated) {
    return { state: "connected", label: "Connected", tone: "ok", socket: "Open · authenticated" };
  }
  if (socketState === 1) {
    return { state: "connecting", label: "Authenticating", tone: "warn", socket: "Open · handshaking" };
  }
  if (socketState === 0) {
    return { state: "connecting", label: "Connecting", tone: "warn", socket: "Connecting" };
  }
  const socketLabel = SOCKET_LABELS[socketState] || `Unknown (${status.socketState})`;
  if (hasError) {
    return { state: "error", label: "Error", tone: "bad", socket: socketLabel };
  }
  return { state: "disconnected", label: "Disconnected", tone: "bad", socket: socketLabel };
}

function renderStatus(status) {
  if (!status || typeof status !== "object") {
    renderUnavailable(new Error("The background service worker returned an empty status."));
    return;
  }

  const connection = describeConnection(status);
  setBadge(connection.state, connection.label);

  setText(dom.socketValue, connection.socket);
  setValueTone(dom.socketValue, connection.tone);

  setText(dom.sessionValue, status.sessionId ? middleTruncate(status.sessionId, 8, 4) : "none");
  dom.sessionValue.title = status.sessionId || "";

  const snapshot = status.latestSnapshot || null;
  setText(dom.snapshotCount, Number(status.snapshotCount) || 0);
  setText(dom.elementCount, snapshot ? (snapshot.elements?.length ?? 0) : "—");

  const errorText = status.lastBridgeError ? String(status.lastBridgeError) : "";

  // "No pairing code set" is not a fault to report, it is a step the user has
  // not been able to take. Keep the button offered for every pairing-shaped
  // failure too — a denied or expired request must still be retryable.
  const unpaired = /no pairing code/i.test(errorText);
  const pairingFailed = /pairing|invalid bridge token/i.test(errorText);
  const needsPairing = !status.authenticated && (unpaired || pairingFailed);
  dom.pairBtn.hidden = !needsPairing;
  dom.pairBtn.textContent = unpaired ? "Pair this extension…" : "Try pairing again…";
  dom.activityNote.className = errorText ? "note is-error" : "note";
  setText(
    dom.activityNote,
    unpaired
      ? "Not paired yet. Click below, then approve the request in the ClarityOS window."
      : errorText || status.lastBridgeMessage || "No bridge activity yet.",
  );

  renderSnapshot(snapshot);
  renderDiagnostics(status, snapshot, errorText);
}

function renderSnapshot(snapshot) {
  if (!snapshot) {
    dom.snapshotBody.hidden = true;
    dom.snapshotEmpty.hidden = false;
    return;
  }

  dom.snapshotBody.hidden = false;
  dom.snapshotEmpty.hidden = true;

  setText(dom.tabTitle, snapshot.title || "Untitled page");
  dom.tabTitle.title = snapshot.title || "";

  const url = String(snapshot.url || "");
  const safe = /^https?:\/\//i.test(url);
  dom.tabUrl.textContent = url || "—";
  dom.tabUrl.title = url;
  if (safe) {
    dom.tabUrl.href = url;
    dom.tabUrl.removeAttribute("aria-disabled");
  } else {
    dom.tabUrl.removeAttribute("href");
    dom.tabUrl.setAttribute("aria-disabled", "true");
  }

  setText(dom.tabCaptured, `Captured ${relativeTime(snapshot.generation)}`);
  setText(dom.tabElements, `${snapshot.elements?.length ?? 0} elements`);
}

function renderDiagnostics(status, snapshot, errorText) {
  setText(dom.diagUrl, status.bridgeUrl);
  setText(dom.diagExt, status.extensionName);
  setText(
    dom.diagSocket,
    `${status.socketState} (${SOCKET_LABELS[Number(status.socketState)] || "unknown"})`,
  );
  setText(dom.diagAuth, status.authenticated ? "yes" : "no");
  setText(dom.diagSession, status.sessionId || "none");
  dom.diagSession.title = status.sessionId || "";
  setText(dom.diagTab, snapshot?.tab_id);
  setText(dom.diagGen, snapshot?.generation);
  setText(dom.diagError, errorText || "none");
  dom.diagError.title = errorText || "";
}

// Nothing answered, so nothing on screen may be presented as live state.
function renderUnavailable(error) {
  setBadge("error", "Unavailable");
  dom.pairBtn.hidden = true;   // no status means no basis for a pairing prompt

  setText(dom.socketValue, "Unknown");
  setValueTone(dom.socketValue, "bad");
  setText(dom.sessionValue, "—");
  setText(dom.snapshotCount, "—");
  setText(dom.elementCount, "—");

  dom.activityNote.className = "note is-error";
  const reason = error?.message || String(error);
  // Both failure shapes have the same remedy: the worker's registration is
  // stale (editing background.js does not re-register it — only Reload does).
  const unresponsive = /receiving end|establish connection|did not respond/i.test(reason);
  setText(
    dom.activityNote,
    unresponsive
      ? `${reason} Open chrome://extensions and press Reload on ClarityOS Browser Bridge, then refresh here.`
      : reason,
  );

  renderSnapshot(null);
  for (const node of [dom.diagUrl, dom.diagExt, dom.diagSocket, dom.diagAuth, dom.diagSession, dom.diagTab, dom.diagGen]) {
    setText(node, "—");
  }
  setText(dom.diagError, reason);
  dom.diagError.title = reason;
}

// ── Actions ──

async function loadStatus({ announce = false } = {}) {
  if (inFlight) return;
  setBusy(true, announce ? dom.refreshBtn : null);
  if (announce) setFeedback("");
  try {
    const status = await sendWithWake({ type: "clarityos_get_status" }, STATUS_TIMEOUT_MS);
    renderStatus(status);
    if (announce) setFeedback("Status refreshed.", "ok");
  } catch (error) {
    renderUnavailable(error);
    if (announce) setFeedback(error?.message || String(error), "error");
  } finally {
    setBusy(false);
  }
}

async function requestSnapshot() {
  if (inFlight) return;
  setBusy(true, dom.snapshotBtn);
  setFeedback("Requesting a snapshot of the active tab…");
  try {
    const response = await sendWithWake({ type: "clarityos_request_snapshot" }, SNAPSHOT_TIMEOUT_MS);
    renderStatus(response);
    if (response?.ok) {
      setFeedback("Snapshot captured.", "ok");
    } else if (response?.restricted) {
      // Chrome's own pages can never be read. Say why, without calling it an error.
      setFeedback(response.note || "Chrome blocks page access on this tab. Switch to a normal site tab.");
    } else {
      setFeedback(response?.error || response?.lastBridgeError || "Snapshot request failed.", "error");
    }
  } catch (error) {
    renderUnavailable(error);
    setFeedback(error?.message || String(error), "error");
  } finally {
    setBusy(false);
  }
}

// Ask the hub to pair us. The code is released only when the user approves the
// prompt that appears in the ClarityOS window — nothing is granted here.
dom.pairBtn.addEventListener("click", async () => {
  if (inFlight) return;
  setBusy(true, null);
  dom.pairBtn.disabled = true;
  setBadge("connecting", "Awaiting approval");
  setFeedback("Approve the request in the ClarityOS window…");
  try {
    await sendWithWake({ type: "clarityos_request_pairing" }, STATUS_TIMEOUT_MS);
    // The approval is a human round-trip; poll until it settles or expires.
    for (let i = 0; i < 40; i += 1) {
      await new Promise((r) => setTimeout(r, 1500));
      const status = await sendMessage({ type: "clarityos_get_status" }, STATUS_TIMEOUT_MS).catch(() => null);
      if (!status) continue;
      renderStatus(status);
      if (status.authenticated) { setFeedback("Paired and connected.", "ok"); return; }
      if (status.lastBridgeError) { setFeedback(status.lastBridgeError, "error"); return; }
    }
    setFeedback("Pairing request expired. Try again, or paste the code in Options.", "error");
  } catch (error) {
    setFeedback(error?.message || String(error), "error");
  } finally {
    dom.pairBtn.disabled = false;
    setBusy(false);
    loadStatus();
  }
});

dom.refreshBtn.addEventListener("click", () => loadStatus({ announce: true }));
dom.snapshotBtn.addEventListener("click", requestSnapshot);

// Sending any message wakes a terminated service worker, whose boot path runs
// loadSettings().then(connectBridge) — so opening the popup also revives a
// dropped bridge connection. The follow-up read reports the result of that.
loadStatus().then(() => {
  setTimeout(() => {
    if (!inFlight) loadStatus();
  }, 1200);
});
