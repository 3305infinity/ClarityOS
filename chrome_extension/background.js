// ═══════════════════════════════════════════════════════════════
//  ClarityOS — Background Service Worker
//  Manages WebSocket bridge to the ClarityOS backend, action
//  polling, snapshot forwarding, and keepalive pings.
// ═══════════════════════════════════════════════════════════════

// Defaults — overridden by chrome.storage.sync settings
const DEFAULT_BRIDGE_URL = "ws://127.0.0.1:8765";
const DEFAULT_BRIDGE_TOKEN = "";  // no default: pair via ClarityOS website → Settings → Connections
const EXTENSION_NAME = "ClarityOS companion";

const ACTION_POLL_INTERVAL_MS = 5000;  // Fallback only — actions pushed via WebSocket
const KEEPALIVE_INTERVAL_MS = 15000;   // ping every 15 s
const RECONNECT_DELAY_MS = 1500;
const MAX_RECONNECT_DELAY_MS = 30000;  // backoff ceiling when the hub is down
const BRIDGE_ALARM = "clarityos-bridge-keepalive";

// Mutable config (loaded from chrome.storage.sync)
let BRIDGE_URL = DEFAULT_BRIDGE_URL;
let BRIDGE_TOKEN = DEFAULT_BRIDGE_TOKEN;

let bridgeSocket = null;
let authenticated = false;
let sessionId = `chrome-session-${Date.now()}`;
let reconnectTimer = null;
let reconnectAttempts = 0;
let latestSnapshotByTab = new Map();
let lastBridgeError = "";
let lastBridgeMessage = "Not connected yet";
let actionPollTimer = null;
let keepaliveTimer = null;
let actionExecutionInFlight = false;
// True only when neither storage area could be read, so an empty token is not
// mistaken for "the user has not paired yet".
let settingsUnreadable = false;
// Set only by an explicit user action in the popup. An unpaired extension must
// never prompt the hub on its own reconnect timer.
let pairingRequested = false;

// ── Load Settings ──

async function readArea(area) {
  try {
    return await chrome.storage[area].get(["bridgeUrl", "bridgeToken"]);
  } catch (error) {
    return { __error: String(error?.message || error) };
  }
}

/**
 * Resolve the bridge URL and pairing code.
 *
 * The options page writes both chrome.storage.sync (so the pairing follows a
 * signed-in profile) and chrome.storage.local (so it keeps working when sync is
 * disabled by policy, out of quota, or simply unavailable). Sync wins when it
 * has a value; local is the fallback. Neither area's empty default is ever
 * allowed to clear a code the other area still holds.
 *
 * The pairing code itself is never logged.
 */
async function loadSettings() {
  const [sync, local] = await Promise.all([readArea("sync"), readArea("local")]);

  if (sync.__error && local.__error) {
    // Both areas unreadable. Reporting "No pairing code set" here used to send
    // people off to re-paste a code that was never the problem.
    settingsUnreadable = true;
    lastBridgeError = `Could not read extension settings: ${sync.__error}`;
    lastBridgeMessage = "Extension settings are unreadable; pairing cannot be checked";
    log("Failed to read settings from both sync and local storage");
    return;
  }
  settingsUnreadable = false;

  BRIDGE_URL = sync.bridgeUrl || local.bridgeUrl || DEFAULT_BRIDGE_URL;
  BRIDGE_TOKEN = sync.bridgeToken || local.bridgeToken || DEFAULT_BRIDGE_TOKEN;
  // Presence only — never the code itself. "Settings loaded" on its own could
  // not distinguish "read fine, nothing stored" from "read failed".
  log(
    `Settings loaded: url=${BRIDGE_URL} pairingCode=${BRIDGE_TOKEN ? "present" : "absent"}` +
    ` (sync=${sync.bridgeToken ? "yes" : "no"}, local=${local.bridgeToken ? "yes" : "no"})`,
  );

  // Heal a half-written pair (e.g. a code saved before the local mirror
  // existed) so the next worker start succeeds from either area.
  if (BRIDGE_TOKEN && !local.__error && !local.bridgeToken) {
    try {
      await chrome.storage.local.set({ bridgeUrl: BRIDGE_URL, bridgeToken: BRIDGE_TOKEN });
    } catch (_) {
      // Mirroring is best-effort; sync still has the code.
    }
  }
}

// ── Utility ──

function isInjectableUrl(url) {
  if (!url || typeof url !== "string") return false;
  return /^(https?|file):/i.test(url);
}

/** Origin-only form of a URL, for user-facing messages. */
function shortUrl(url) {
  try {
    const parsed = new URL(String(url));
    return parsed.protocol === "chrome:" || parsed.protocol === "edge:"
      ? `${parsed.protocol}//${parsed.hostname}`
      : parsed.origin;
  } catch (_) {
    return "this page";
  }
}

async function ensureContentScript(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["Readability.js", "content_script.js"],
  });
}

function log(...args) {
  console.log("[ClarityOS Bridge]", ...args);
}

// ── Keepalive ──

function startKeepalive() {
  if (keepaliveTimer) return;
  keepaliveTimer = setInterval(() => {
    if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN && authenticated) {
      bridgeSocket.send(JSON.stringify({ type: "browser_ping" }));
    }
  }, KEEPALIVE_INTERVAL_MS);
}

function stopKeepalive() {
  if (keepaliveTimer) {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
  }
}

// ── Action Polling ──

function stopActionPolling() {
  if (actionPollTimer) {
    clearInterval(actionPollTimer);
    actionPollTimer = null;
  }
}

function startActionPolling() {
  if (actionPollTimer) return;
  actionPollTimer = setInterval(() => {
    pollBridgeActions().catch((error) => {
      lastBridgeError = String(error?.message || error);
      lastBridgeMessage = "Action polling failed";
      log("Action polling failed", error);
    });
  }, ACTION_POLL_INTERVAL_MS);
}

async function pollBridgeActions() {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN || !authenticated || actionExecutionInFlight) {
    return;
  }
  bridgeSocket.send(
    JSON.stringify({
      type: "browser_poll_actions",
      session_id: sessionId,
    }),
  );
}

// ── Action Execution ──

async function executeActionOnTab(action) {
  const metadata = action?.metadata || {};
  const targetTabId = Number(metadata.tab_id || latestSnapshotByTab.keys().next().value || 0);
  const tab = targetTabId ? await chrome.tabs.get(targetTabId).catch(() => null) : await getActiveTab();

  if (!tab?.id) {
    return buildActionResult(action, false, "No browser tab available for action execution.", metadata, { reason: "missing-tab" });
  }

  if (!isInjectableUrl(tab.url)) {
    return buildActionResult(action, false, `Cannot execute browser action on ${tab.url || "this page"}.`, metadata, {
      reason: "unsupported-url",
      tab_id: String(tab.id),
    });
  }

  try {
    await ensureContentScript(tab.id);

    if (action?.action === "refresh_snapshot") {
      const refreshed = await requestSnapshotFromTab(tab.id, action?.session_id || sessionId);
      return buildActionResult(
        action,
        !!refreshed?.ok,
        refreshed?.ok ? "Browser snapshot refreshed." : "Browser snapshot refresh failed.",
        metadata,
        { tab_id: String(tab.id), refreshed: String(!!refreshed?.ok) },
        Date.now(),
      );
    }

    // ── Scroll action: relay to content script via clarityos_scroll ──
    if (action?.action === "scroll") {
      const scrollResult = await chrome.tabs.sendMessage(tab.id, {
        type: "clarityos_scroll",
        direction: metadata.direction || "down",
        amount: metadata.amount || "page",
        sessionId: action?.session_id || sessionId,
        tabId: String(tab.id),
      });
      return buildActionResult(
        action,
        !!scrollResult?.ok,
        scrollResult?.ok
          ? `Scrolled ${metadata.direction || "down"} by ${metadata.amount || "page"}.`
          : "Scroll action failed.",
        metadata,
        {
          tab_id: String(tab.id),
          scrollY: String(scrollResult?.scrollY || 0),
          pageHeight: String(scrollResult?.pageHeight || 0),
          atBottom: String(!!scrollResult?.atBottom),
          atTop: String(!!scrollResult?.atTop),
        },
        Date.now(),
      );
    }

    // ── Highlight action: visually mark elements the agent is reading ──
    if (action?.action === "highlight") {
      const highlightResult = await chrome.tabs.sendMessage(tab.id, {
        type: "clarityos_highlight",
        agentIds: metadata.agent_ids || [],
        duration: Number(metadata.duration || 3000),
        mode: metadata.mode || "reading",
        tool: metadata.tool || "",
        title: metadata.title || "",
        sourceUrl: metadata.source_url || "",
        snippet: metadata.snippet || "",
        itemCount: Number(metadata.item_count || 0),
      });
      return buildActionResult(
        action,
        !!highlightResult?.ok,
        highlightResult?.ok
          ? `Highlighted ${highlightResult.highlighted || 0} elements.`
          : "Highlight action failed.",
        metadata,
        {
          tab_id: String(tab.id),
          highlighted: String(highlightResult?.highlighted || 0),
          overlay_visible: String(!!highlightResult?.overlayVisible),
        },
        Date.now(),
      );
    }

    // ── Scanning mode: page-wide AI reading/analysis visual ──
    if (action?.action === "scanning_start") {
      const scanResult = await chrome.tabs.sendMessage(tab.id, {
        type: "clarityos_scanning_start",
        label: metadata.label || "AI analyzing page…",
        duration: Number(metadata.duration || 4000),
      });
      return buildActionResult(
        action,
        !!scanResult?.ok,
        scanResult?.ok
          ? `Scanning started, highlighted ${scanResult.highlighted || 0} elements.`
          : "Scanning start failed.",
        metadata,
        { tab_id: String(tab.id), highlighted: String(scanResult?.highlighted || 0) },
        Date.now(),
      );
    }
    if (action?.action === "scanning_stop") {
      const stopResult = await chrome.tabs.sendMessage(tab.id, {
        type: "clarityos_scanning_stop",
      });
      return buildActionResult(
        action,
        !!stopResult?.ok,
        "Scanning stopped.",
        metadata,
        { tab_id: String(tab.id) },
        Date.now(),
      );
    }

    // ── Extract Data action: safely extract specific DOM strings without eval() ──
    if (action?.action === "extract_data") {
      const evalResult = await chrome.tabs.sendMessage(tab.id, {
        type: "clarityos_extract_data",
        target: action?.text || "",
        sessionId: action?.session_id || sessionId,
        tabId: String(tab.id),
      });
      return buildActionResult(
        action,
        !!evalResult?.ok,
        evalResult?.ok ? "Data extraction successful." : "Data extraction failed.",
        metadata,
        {
          tab_id: String(tab.id),
          result: evalResult?.result || "",
          error: evalResult?.error || "",
        },
        Date.now(),
      );
    }

    if (action?.action === "extract_readability") {
      const readabilityResult = await chrome.tabs.sendMessage(tab.id, {
        type: "clarityos_extract_readability",
        sessionId: action?.session_id || sessionId,
        tabId: String(tab.id),
      });
      return buildActionResult(
        action,
        !!readabilityResult?.ok,
        readabilityResult?.message || (readabilityResult?.ok ? "Readability extraction successful." : "Readability extraction failed."),
        metadata,
        {
          tab_id: String(tab.id),
          result: JSON.stringify(readabilityResult || {}),
          error: readabilityResult?.error || "",
        },
        Date.now(),
      );
    }

    // ── Click pointer: show agent cursor before click actions ──
    const isClickLike = ["click", "type", "fill", "select"].includes(action?.action);
    if (isClickLike && action?.ref_id) {
      try {
        const snapshot = latestSnapshotByTab.get(tab.id) || [...latestSnapshotByTab.values()].slice(-1)[0];
        if (snapshot?.elements) {
          const el = snapshot.elements.find(e => e.ref_id === action.ref_id);
          const b = el?.bounds;
          if (b && (b.width > 0 || b.height > 0)) {
            const pageX = (b.x || 0) + (b.width || 0) / 2;
            const pageY = (b.y || 0) + (b.height || 0) / 2;
            chrome.tabs.sendMessage(tab.id, {
              type: "clarityos_show_click_pointer",
              pageX,
              pageY,
            }).catch(() => {});
          }
        }
      } catch (_) {}
    }

    const result = await chrome.tabs.sendMessage(tab.id, {
      type: "clarityos_execute_action",
      action,
      sessionId: action?.session_id || sessionId,
      tabId: String(tab.id),
    });

    // After a successful click, fire the burst animation
    if (result?.ok && isClickLike) {
      chrome.tabs.sendMessage(tab.id, { type: "clarityos_trigger_click_burst" }).catch(() => {});
    }

    // After a successful action, the content script's MutationObserver
    // will fire a snapshot automatically — no explicit request needed.

    return buildActionResult(
      action,
      !!result?.ok,
      result?.message || (result?.ok ? "Browser action executed." : "Browser action failed."),
      metadata,
      {
        tab_id: String(tab.id),
        executed_ref_id: result?.executedRefId || action?.ref_id || "",
        matched_by: result?.matchedBy || "",
      },
      Date.now(),
    );
  } catch (error) {
    return buildActionResult(action, false, String(error?.message || error), metadata, {
      reason: "execution-error",
      tab_id: String(tab?.id || ""),
    });
  }
}

function buildActionResult(action, ok, message, metadata, details, postGen) {
  const errorCode = ok ? "" : String(details?.error_code || details?.reason || "bridge.action_failed");
  return {
    ok,
    message,
    action: action?.action || "",
    ref_id: action?.ref_id || "",
    action_id: action?.action_id || "",
    session_id: action?.session_id || sessionId,
    pre_generation: Number(metadata?.generation || 0),
    post_generation: postGen ?? Number(metadata?.generation || 0),
    details: details || {},
    error: ok ? null : {
      code: errorCode,
      message,
      retryable: false,
      degraded: false,
      source: "bridge.background",
      details: details || {},
    },
    meta: {
      session_id: action?.session_id || sessionId,
      provenance: "chrome_extension",
    },
  };
}

async function processBridgeActions(actions) {
  if (!Array.isArray(actions) || actions.length === 0 || actionExecutionInFlight) {
    return;
  }

  actionExecutionInFlight = true;
  try {
    for (const action of actions) {
      lastBridgeMessage =
        action.action === "refresh_snapshot"
          ? "Refreshing browser snapshot"
          : `Executing ${action.action} on ${action.ref_id}`;
      const result = await executeActionOnTab(action);
      sendToBridge({ type: "browser_action_result", result });
      lastBridgeMessage = result.ok ? `Executed ${action.action} on ${action.ref_id}` : `Action failed for ${action.ref_id}`;
      if (!result.ok) {
        lastBridgeError = result.message;
      }
    }
  } finally {
    actionExecutionInFlight = false;
  }
}

// ── Snapshot Requests ──

async function requestSnapshotFromTab(tabId, sessionIdOverride = sessionId) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.id) {
    lastBridgeError = "No active tab available for snapshot";
    lastBridgeMessage = "Snapshot request skipped";
    return { ok: false, reason: "missing-tab" };
  }

  if (!isInjectableUrl(tab.url)) {
    // Chrome forbids content scripts on its own pages. That is a property of
    // the tab, not a bridge fault, so it must not land in lastBridgeError and
    // light the popup up red.
    lastBridgeMessage = `Chrome does not allow page access on ${shortUrl(tab.url)} — open a normal site tab.`;
    return { ok: false, reason: "restricted-url", restricted: true, url: tab.url || "" };
  }

  const payload = {
    type: "clarityos_collect_snapshot",
    sessionId: sessionIdOverride,
    tabId: String(tab.id),
  };

  try {
    const response = await chrome.tabs.sendMessage(tab.id, payload);
    lastBridgeError = "";
    lastBridgeMessage = "Snapshot request delivered";
    return { ok: !!response?.ok, injected: false };
  } catch (error) {
    const message = String(error?.message || error);
    if (!/Receiving end does not exist/i.test(message)) {
      lastBridgeError = message;
      lastBridgeMessage = "Snapshot request failed";
      return { ok: false, reason: "send-failed", error: message };
    }
  }

  // Content script not yet injected — inject and retry
  try {
    await ensureContentScript(tab.id);
    const response = await chrome.tabs.sendMessage(tab.id, payload);
    lastBridgeError = "";
    lastBridgeMessage = "Content script injected; snapshot requested";
    return { ok: !!response?.ok, injected: true };
  } catch (error) {
    const message = String(error?.message || error);
    lastBridgeError = message;
    lastBridgeMessage = "Content script injection failed";
    return { ok: false, reason: "inject-failed", error: message };
  }
}

// ── Bridge Connection ──

function bridgeStatus() {
  const latestSnapshot = [...latestSnapshotByTab.values()].slice(-1)[0] || null;
  return {
    authenticated,
    sessionId,
    bridgeUrl: BRIDGE_URL,
    extensionName: EXTENSION_NAME,
    socketState: bridgeSocket ? bridgeSocket.readyState : WebSocket.CLOSED,
    lastBridgeError,
    lastBridgeMessage,
    latestSnapshot,
    snapshotCount: latestSnapshotByTab.size,
  };
}

function connectBridge() {
  if (bridgeSocket && (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  // Without a pairing code the handshake can only ever be rejected. Say so
  // plainly instead of looping on a generic "Invalid bridge token".
  if (!BRIDGE_TOKEN || !BRIDGE_TOKEN.trim()) {
    // Keep the storage-read failure if that is why the token is empty —
    // telling the user to re-paste a code they already saved wastes their time.
    if (!settingsUnreadable) {
      lastBridgeError = "No pairing code set";
      lastBridgeMessage =
        "Not paired. Click “Pair this extension” in the popup, then approve the request " +
        "in the ClarityOS window.";
      log(lastBridgeMessage);
    }
    // Only open a socket to ask for pairing when the user actually asked us to;
    // otherwise an unpaired extension would prompt the hub on every retry.
    if (pairingRequested) {
      openSocket({ pairing: true });
      return;
    }
    scheduleReconnect();           // picks up the code as soon as it is saved
    return;
  }

  openSocket({ pairing: false });
}

// One socket, two opening moves: a normal authenticated hello, or — when the
// user has asked to pair and we hold no code — a pairing request that the hub
// must have a human approve.
function openSocket({ pairing }) {
  bridgeSocket = new WebSocket(BRIDGE_URL);

  bridgeSocket.addEventListener("open", () => {
    authenticated = false;
    lastBridgeError = "";
    if (pairing) {
      lastBridgeMessage = "Waiting for approval in the ClarityOS window…";
      log("Socket open; requesting pairing approval");
      bridgeSocket.send(JSON.stringify({
        type: "browser_pair_request",
        extension_name: EXTENSION_NAME,
      }));
      return;
    }
    lastBridgeMessage = "Socket open; sending hello";
    log("Connected to backend bridge");
    bridgeSocket.send(
      JSON.stringify({
        type: "browser_bridge_hello",
        token: BRIDGE_TOKEN,
        session_id: sessionId,
        extension_name: EXTENSION_NAME,
      }),
    );
  });

  bridgeSocket.addEventListener("message", async (event) => {
    try {
      const message = JSON.parse(event.data);

      if (message.type === "browser_bridge_hello_ack") {
        authenticated = !!message.ok;
        lastBridgeMessage = message.ok
          ? "Handshake accepted"
          : `Handshake rejected: ${message.message || "unknown error"}. ` +
            `The pairing code in this extension's Options does not match the one in ` +
            `ClarityOS → Settings → Connections — copy it again (it changes if you rotate it).`;
        log("Handshake ack", message);
        if (authenticated) {
          reconnectAttempts = 0;        // healthy again — reset the backoff
          startActionPolling();
          startKeepalive();
          await pushActiveTabSnapshot();
          await pollBridgeActions();
        } else {
          lastBridgeError = message.message || "Handshake rejected";
          try {
            bridgeSocket?.close();
          } catch (_) {}
          scheduleReconnect();
        }
        return;
      }

      if (message.type === "browser_pair_result") {
        pairingRequested = false;
        if (message.ok && message.token) {
          // Persist to both areas, then authenticate normally. The code is
          // never logged; only the outcome is.
          const granted = String(message.token);
          try {
            await Promise.allSettled([
              chrome.storage.sync.set({ bridgeUrl: BRIDGE_URL, bridgeToken: granted }),
              chrome.storage.local.set({ bridgeUrl: BRIDGE_URL, bridgeToken: granted }),
            ]);
          } catch (_) {
            // Stored or not, we can still use it for this session.
          }
          BRIDGE_TOKEN = granted;
          lastBridgeError = "";
          lastBridgeMessage = "Pairing approved; authenticating";
          log("Pairing approved by the user");
          bridgeSocket.send(JSON.stringify({
            type: "browser_bridge_hello",
            token: BRIDGE_TOKEN,
            session_id: sessionId,
            extension_name: EXTENSION_NAME,
          }));
        } else {
          lastBridgeError = message.message || "Pairing was not approved";
          lastBridgeMessage = "Not paired";
          log(`Pairing refused: ${lastBridgeError}`);
          try { bridgeSocket?.close(); } catch (_) {}
        }
        return;
      }

      if (message.type === "browser_snapshot_ack") {
        lastBridgeMessage = `Snapshot accepted (${message.elements || 0} elements)`;
        return;
      }

      if (message.type === "browser_action_result_ack") {
        lastBridgeMessage = `Action result acknowledged (${message.action_id || "unknown"})`;
        return;
      }

      if (message.type === "browser_pong") {
        // Keepalive response — connection healthy
        return;
      }

      if (message.type === "browser_actions" && Array.isArray(message.actions)) {
        lastBridgeMessage = `Received ${message.actions.length} action(s)`;
        log("Received browser actions", message.actions);
        await processBridgeActions(message.actions);
      }
    } catch (error) {
      lastBridgeError = String(error?.message || error);
      log("Failed to process bridge message", error);
    }
  });

  bridgeSocket.addEventListener("close", () => {
    authenticated = false;
    bridgeSocket = null;              // drop the dead socket so connectBridge() can retry
    lastBridgeMessage = "Socket closed; reconnect scheduled";
    stopActionPolling();
    stopKeepalive();
    log("Bridge connection closed; scheduling reconnect");
    scheduleReconnect();
  });

  bridgeSocket.addEventListener("error", (error) => {
    authenticated = false;
    lastBridgeError = String(error?.message || "Bridge socket error");
    lastBridgeMessage = "Socket error";
    stopActionPolling();
    stopKeepalive();
    log("Bridge error", error);
    // `error` is not always followed by `close` (a failed handshake can end
    // here), and without this the extension stayed down until something else
    // happened to wake the worker.
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  // Bounded exponential backoff so a stopped hub is not hammered every 1.5 s.
  const delay = Math.min(RECONNECT_DELAY_MS * Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY_MS);
  reconnectAttempts++;
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    // Re-read settings before every attempt. Without this the worker kept
    // retrying with the token it had in memory at startup, so pasting a new
    // pairing code (or rotating it in the hub) never took effect until the
    // service worker happened to restart.
    await loadSettings();
    connectBridge();
  }, delay);
}

/**
 * Reconnect if the persistent socket is not open.
 *
 * This is the fix for the real problem: an MV3 service worker is terminated
 * after ~30 s idle, which destroys the WebSocket *and* every setInterval
 * (keepalive, action polling). Nothing then woke the worker, so the bridge
 * stayed down until some unrelated event revived it — meanwhile snapshots
 * still worked, because the content script → worker path is separate from the
 * socket. A chrome.alarm is the supported way to wake an MV3 worker on a timer.
 */
function ensureBridgeConnected(reason) {
  if (bridgeSocket &&
      (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  log(`Bridge not connected (${reason}) — reconnecting`);
  reconnectAttempts = 0;              // an alarm tick is a fresh chance, not a retry storm
  loadSettings().then(() => connectBridge());
}

chrome.alarms.create(BRIDGE_ALARM, { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === BRIDGE_ALARM) ensureBridgeConnected("alarm tick");
});

function sendToBridge(payload) {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN || !authenticated) {
    lastBridgeMessage = "Bridge send skipped; socket not ready or unauthenticated";
    return false;
  }
  bridgeSocket.send(JSON.stringify(payload));
  lastBridgeMessage = `Sent ${payload.type}`;
  return true;
}

// ── Tab Helpers ──

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tabs[0] || null;
}

async function pushActiveTabSnapshot() {
  const tab = await getActiveTab();
  if (!tab?.id) {
    // Returning silently here made the popup report a successful capture when
    // nothing had been captured at all.
    lastBridgeError = "No active tab to snapshot";
    lastBridgeMessage = "Snapshot request skipped";
    return { ok: false, reason: "no-active-tab" };
  }
  const result = await requestSnapshotFromTab(tab.id, sessionId);
  if (!result.ok) {
    log("Could not request content snapshot", result);
  }
  return result;
}

// ── Chrome Event Listeners ──

chrome.runtime.onInstalled.addListener(() => {
  loadSettings().then(() => connectBridge());
});

chrome.runtime.onStartup.addListener(() => {
  loadSettings().then(() => connectBridge());
});

chrome.tabs.onActivated.addListener(() => {
  if (authenticated) {
    pushActiveTabSnapshot();
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (authenticated && changeInfo.status === "complete") {
    requestSnapshotFromTab(tabId, sessionId).catch((error) => {
      log("Snapshot refresh failed after tab update", error);
    });
  }
});

// ── Internal Message Handling ──

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "clarityos_snapshot") {
    latestSnapshotByTab.set(message.snapshot?.tab_id || sender.tab?.id || "unknown", message.snapshot);
    const ok = sendToBridge({
      type: "browser_snapshot",
      snapshot: message.snapshot,
    });
    sendResponse({ ok, authenticated });
    return true;
  }

  if (message?.type === "clarityos_dom_change") {
    // Forward DOM mutation events from content script to the backend bridge
    const ok = sendToBridge({
      type: "browser_dom_change",
      event: message.event,
    });
    sendResponse({ ok, authenticated });
    return true;
  }

  if (message?.type === "clarityos_request_pairing") {
    // Explicit user action: open a socket and ask the hub for approval.
    pairingRequested = true;
    lastBridgeError = "";
    lastBridgeMessage = "Asking ClarityOS to approve this extension…";
    if (bridgeSocket) { try { bridgeSocket.close(); } catch (_) {} }
    bridgeSocket = null;
    authenticated = false;
    loadSettings().then(() => connectBridge());
    sendResponse({ ok: true, ...bridgeStatus() });
    return true;
  }

  if (message?.type === "clarityos_get_status") {
    sendResponse(bridgeStatus());
    return true;
  }

  if (message?.type === "clarityos_request_snapshot") {
    // Report what actually happened, not merely that the attempt finished.
    pushActiveTabSnapshot()
      .then((result) =>
        sendResponse({
          ok: !!result?.ok,
          // A restricted page is a limitation to explain, not a failure to report.
          restricted: !!result?.restricted,
          error: result?.ok || result?.restricted
            ? ""
            : result?.error || result?.reason || lastBridgeError || "Snapshot failed",
          note: result?.restricted ? lastBridgeMessage : "",
          ...bridgeStatus(),
        }),
      )
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error), ...bridgeStatus() }));
    return true;
  }

  if (message?.type === "clarityos_ping_bridge") {
    const ok = sendToBridge({ type: "browser_ping" });
    sendResponse({ ok, authenticated, sessionId });
    return true;
  }

  if (message?.type === "clarityos_settings_updated") {
    // Settings changed from options page — reconnect with new config
    loadSettings().then(() => {
      if (bridgeSocket) {
        try { bridgeSocket.close(); } catch (_) {}
      }
      bridgeSocket = null;
      authenticated = false;
      connectBridge();
    });
    sendResponse({ ok: true });
    return true;
  }

  return false;
});

// ── Boot ──
loadSettings().then(() => connectBridge());
