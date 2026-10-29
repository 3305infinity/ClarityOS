// Drives the real extension in real Chrome over CDP and reports, with timings:
//   1. does the MV3 service worker register and start?
//   2. what alarms does it actually register?
//   3. does popup.html render a real status (warm worker)?
//   4. what happens when the worker has been terminated (the screenshot case)?
//
// Evidence only — this changes nothing. Run: node tests/extension/live_chrome_probe.js
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const WebSocket = require("ws");

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const EXT = path.resolve(__dirname, "..", "..", "chrome_extension");
const PORT = 9336;
const IDLE_WAIT_MS = Number(process.env.IDLE_WAIT_MS || 45000);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url, { maxPayload: 1 << 24 });
    ws.on("open", () => resolve(ws));
    ws.on("error", reject);
  });
}

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
    return res.result.value !== undefined ? res.result.value : res.result.description;
  } finally {
    ws.close();
  }
}

const TIMED_SEND = `(async () => {
  const t0 = performance.now();
  try {
    const r = await chrome.runtime.sendMessage({ type: "moonwalk_get_status" });
    return JSON.stringify({ ms: Math.round(performance.now() - t0), got: !!r, socketState: r && r.socketState, msg: r && r.lastBridgeMessage });
  } catch (e) {
    return JSON.stringify({ ms: Math.round(performance.now() - t0), error: String(e && e.message || e) });
  }
})()`;

(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "clarity-probe-"));
  const chrome = spawn(CHROME, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--headless=new", "--no-first-run", "--no-default-browser-check", "about:blank",
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
    console.log(`extension id: ${id}`);
    await sleep(2500);

    let targets = await list();
    let worker = targets.find((t) => t.type === "service_worker" && t.url.includes(id));
    console.log(`1. worker after install: ${worker ? "RUNNING" : "ABSENT"}`);

    if (worker) {
      console.log(`2. alarms: ${await evaluate(worker, `new Promise(r => chrome.alarms.getAll(a => r(JSON.stringify(a))))`)}`);
      console.log(`   status: ${await evaluate(worker, `JSON.stringify(bridgeStatus())`, false)}`);
    }

    // Open the popup page for real.
    const page = targets.find((t) => t.type === "page");
    const pageWs = await connect(page.webSocketDebuggerUrl);
    await rpc(pageWs, "Page.navigate", { url: `chrome-extension://${id}/popup.html` });
    pageWs.close();
    await sleep(3500);

    targets = await list();
    const popup = targets.find((t) => t.url.includes("popup.html"));
    if (!popup) { console.log("popup target not found"); return; }

    console.log(`3. popup shows: ${await evaluate(popup, `JSON.stringify({
      badge: document.getElementById("statusLabel").textContent,
      socket: document.getElementById("socketValue").textContent,
      note: document.getElementById("activityNote").textContent.slice(0, 80),
    })`, false)}`);
    console.log(`   warm get_status: ${await evaluate(popup, TIMED_SEND)}`);

    // Let the worker idle out the way it does in normal use.
    console.log(`4. waiting ${IDLE_WAIT_MS / 1000}s for the worker to be terminated…`);
    await sleep(IDLE_WAIT_MS);
    targets = await list();
    worker = targets.find((t) => t.type === "service_worker" && t.url.includes(id));
    console.log(`   worker now: ${worker ? "STILL RUNNING" : "TERMINATED (inactive)"}`);

    console.log(`   cold get_status: ${await evaluate(popup, TIMED_SEND)}`);
    console.log(`   cold capture_snapshot: ${await evaluate(popup, `(async () => {
      const t0 = performance.now();
      try {
        const r = await chrome.runtime.sendMessage({ type: "moonwalk_request_snapshot" });
        return JSON.stringify({ ms: Math.round(performance.now() - t0), ok: r && r.ok, err: r && r.error });
      } catch (e) { return JSON.stringify({ ms: Math.round(performance.now() - t0), error: String(e && e.message || e) }); }
    })()`)}`);

    targets = await list();
    worker = targets.find((t) => t.type === "service_worker" && t.url.includes(id));
    if (worker) {
      console.log(`5. after wake, alarms: ${await evaluate(worker, `new Promise(r => chrome.alarms.getAll(a => r(JSON.stringify(a))))`)}`);
      console.log(`   status: ${await evaluate(worker, `JSON.stringify(bridgeStatus())`, false)}`);
    }
  } catch (error) {
    console.log("PROBE ERROR:", error.message);
  } finally {
    try { browserWs && browserWs.close(); } catch (_) {}
    chrome.kill();
    process.exit(0);
  }
})();
