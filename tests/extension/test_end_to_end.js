// End-to-end: real Chrome + the real unpacked extension + the real ClarityOS
// bridge on ws://127.0.0.1:8765. Pairs the extension with the hub's pairing code
// (read from disk, never printed) and asserts the popup shows a genuinely
// authenticated socket.
//
//   node tests/extension/test_end_to_end.js
//
// Requires the ClarityOS hub (or voice backend) to be running.
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const WebSocket = require("ws");

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const EXT = path.resolve(__dirname, "..", "..", "chrome_extension");
const PORT = 9337;
const BRIDGE_PORT = 8765;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function tokensPath() {
  const envDir = process.env.CLARITYOS_USER_DIR;
  const base = envDir || path.join(process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming"), "ClarityOS");
  return path.join(base, "tokens.json");
}

function portOpen(port) {
  return new Promise((resolve) => {
    const s = net.connect(port, "127.0.0.1");
    s.on("connect", () => { s.destroy(); resolve(true); });
    s.on("error", () => resolve(false));
    setTimeout(() => { s.destroy(); resolve(false); }, 1500);
  });
}

const connect = (url) => new Promise((resolve, reject) => {
  const ws = new WebSocket(url, { maxPayload: 1 << 24 });
  ws.on("open", () => resolve(ws));
  ws.on("error", reject);
});

function rpc(ws, method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = Math.floor(Math.random() * 1e6);
    const onMsg = (raw) => {
      const m = JSON.parse(raw);
      if (m.id !== id) return;
      ws.off("message", onMsg);
      m.error ? reject(new Error(m.error.message || JSON.stringify(m.error))) : resolve(m.result);
    };
    ws.on("message", onMsg);
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => reject(new Error(`${method} timed out`)), 30000);
  });
}

const list = async () => (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();

async function evaluate(target, expression, awaitPromise = true) {
  const ws = await connect(target.webSocketDebuggerUrl);
  try {
    const res = await rpc(ws, "Runtime.evaluate", { expression, returnByValue: true, awaitPromise });
    if (res.exceptionDetails) return `EXCEPTION: ${res.exceptionDetails.text}`;
    return res.result.value !== undefined ? res.result.value : res.result.description;
  } finally { ws.close(); }
}

(async () => {
  let failed = 0;
  const check = (name, ok, detail = "") => {
    if (!ok) failed += 1;
    console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? `\n        ${detail}` : ""}`);
  };

  if (!(await portOpen(BRIDGE_PORT))) {
    console.log(`  SKIP  bridge not listening on ${BRIDGE_PORT} — start the ClarityOS hub first`);
    process.exit(2);
  }
  const tokens = JSON.parse(fs.readFileSync(tokensPath(), "utf8"));
  const bridgeToken = tokens.bridge;
  if (!bridgeToken) { console.log("  FAIL  no bridge pairing code in tokens.json"); process.exit(1); }
  console.log(`  bridge listening on ${BRIDGE_PORT}; pairing code loaded (${bridgeToken.length} chars, not shown)`);

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "clarity-e2e-"));
  const chrome = spawn(CHROME, [
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    "--headless=new", "--no-first-run", "--no-default-browser-check", "https://example.com/",
  ], { stdio: "ignore" });

  let browserWs;
  try {
    let version;
    for (let i = 0; i < 40; i++) {
      try { version = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; }
      catch (_) { await sleep(250); }
    }
    browserWs = await connect(version.webSocketDebuggerUrl);
    const { id } = await rpc(browserWs, "Extensions.loadUnpacked", { path: EXT });
    await sleep(2000);

    let targets = await list();
    let worker = targets.find((t) => t.type === "service_worker" && t.url.includes(id));
    check("service worker registers and starts", !!worker);
    if (!worker) throw new Error("no worker");

    // Open popup.html in its own tab, leaving the example.com tab in place as
    // the page a snapshot can actually be taken of. A service worker cannot
    // message itself, so every call below runs from the popup — the real path.
    await rpc(browserWs, "Target.createTarget", { url: `chrome-extension://${id}/popup.html` });
    await sleep(2500);
    let popup = (await list()).find((t) => t.url.includes("popup.html"));
    check("popup page opens", !!popup);
    if (!popup) throw new Error("no popup");

    // Pair exactly the way the Options page does.
    await evaluate(popup, `new Promise(r => chrome.storage.sync.set(
      { bridgeUrl: "ws://127.0.0.1:${BRIDGE_PORT}", bridgeToken: ${JSON.stringify(bridgeToken)} }, () => r(1)))`);
    await evaluate(popup, `chrome.runtime.sendMessage({ type: "clarityos_settings_updated" })`);
    await sleep(4000);

    const status = JSON.parse(await evaluate(popup, `(async () => JSON.stringify(
      await chrome.runtime.sendMessage({ type: "clarityos_get_status" })))()`));
    check("authenticated against the real bridge", status.authenticated === true,
      `authenticated=${status.authenticated} socketState=${status.socketState} msg=${status.lastBridgeMessage}`);
    check("socket is actually OPEN (1)", status.socketState === 1, `socketState=${status.socketState}`);

    // Re-open the popup so its UI renders from the now-paired state.
    const popupWs = await connect(popup.webSocketDebuggerUrl);
    await rpc(popupWs, "Page.reload", {});
    popupWs.close();
    await sleep(3000);
    popup = (await list()).find((t) => t.url.includes("popup.html"));
    const shown = JSON.parse(await evaluate(popup, `JSON.stringify({
      badge: document.getElementById("statusLabel").textContent.trim(),
      socket: document.getElementById("socketValue").textContent.trim(),
      session: document.getElementById("sessionValue").textContent.trim(),
      note: document.getElementById("activityNote").textContent.trim(),
      loading: document.body.innerText.includes("Loading…"),
    })`, false));
    check("popup reports Connected from real bridge state", shown.badge === "Connected",
      `badge=${shown.badge} socket=${shown.socket} note=${shown.note}`);
    check("popup never stuck on Loading", shown.loading === false);
    check("session id rendered", shown.session !== "—" && shown.session !== "none", `session=${shown.session}`);

    // Make the real web page the active tab, the way it is when a user clicks
    // the toolbar icon (the popup is not a tab in normal use).
    const webPage = (await list()).find((t) => t.type === "page" && t.url.startsWith("https://"));
    if (webPage) {
      const wp = await connect(webPage.webSocketDebuggerUrl);
      await rpc(wp, "Page.bringToFront", {});
      wp.close();
      await sleep(800);
    }

    const snap = JSON.parse(await evaluate(popup, `(async () => JSON.stringify(
      await chrome.runtime.sendMessage({ type: "clarityos_request_snapshot" })))()`));
    check("capture snapshot succeeds on a real page", snap.ok === true, `ok=${snap.ok} error=${snap.error || ""}`);
    check("snapshot count advanced", (snap.snapshotCount || 0) >= 1, `snapshotCount=${snap.snapshotCount}`);
  } catch (error) {
    console.log("  ERROR:", error.message);
    failed += 1;
  } finally {
    try { browserWs && browserWs.close(); } catch (_) {}
    chrome.kill();
    console.log(failed ? `\n${failed} check(s) failed` : "\nall end-to-end checks passed");
    process.exit(failed ? 1 : 0);
  }
})();
