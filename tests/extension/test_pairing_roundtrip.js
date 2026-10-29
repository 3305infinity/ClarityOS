// Regression test: a pairing code saved through the Options UI must be the code
// the background worker uses — on first start, after a worker restart, and when
// chrome.storage.sync is unavailable.
//
//   node tests/extension/test_pairing_roundtrip.js
//
// The pairing code is never printed; only booleans derived from it are.
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const WebSocket = require("ws");

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const EXT = path.resolve(__dirname, "..", "..", "chrome_extension");
const PORT = 9338;
const SECRET = "test-pairing-code-" + Math.random().toString(36).slice(2, 10);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "clarity-pair-"));
  const chrome = spawn(CHROME, [
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
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
    await sleep(2500);

    // ── 0. Unpaired popup must offer a way to pair ──
    const p0 = await rpc(browserWs, "Target.createTarget", { url: `chrome-extension://${id}/popup.html` });
    await sleep(3000);
    const popup0 = (await list()).find((t) => t.id === p0.targetId);
    const unpaired = JSON.parse(await evaluate(popup0, `JSON.stringify({
      note: document.getElementById("activityNote").textContent.trim(),
      pairHidden: document.getElementById("pairBtn").hidden,
      canOpenOptions: typeof chrome.runtime.openOptionsPage === "function",
    })`, false));
    check("unpaired popup shows a Pair button", unpaired.pairHidden === false, JSON.stringify(unpaired));
    check("unpaired popup says where the code comes from",
      /approve the request in the ClarityOS/i.test(unpaired.note), unpaired.note);
    check("popup can open the options page", unpaired.canOpenOptions === true);
    await rpc(browserWs, "Target.closeTarget", { targetId: p0.targetId }).catch(() => {});

    // ── 1. Save through the real Options UI ──
    await rpc(browserWs, "Target.createTarget", { url: `chrome-extension://${id}/options.html` });
    await sleep(2000);
    let options = (await list()).find((t) => t.url.includes("options.html"));
    check("options page opens", !!options);
    if (!options) throw new Error("no options page");

    check("options form pre-fills from storage on load",
      (await evaluate(options, `document.getElementById("bridgeUrl").value`, false)).startsWith("ws://"),
      "an empty form would let Save wipe a stored code");

    await evaluate(options, `(() => {
      const t = document.getElementById("bridgeToken");
      t.value = ${JSON.stringify(SECRET)};
      t.dispatchEvent(new Event("input", { bubbles: true }));
      document.getElementById("saveBtn").click();
      return true;
    })()`, false);
    await sleep(1500);

    const savedMatches = await evaluate(options, `(async () => {
      const r = await chrome.storage.sync.get(["bridgeToken"]);
      return r.bridgeToken === ${JSON.stringify(SECRET)};
    })()`);
    check("Options writes the code to chrome.storage.sync", savedMatches === true);

    const savedLocal = await evaluate(options, `(async () => {
      const r = await chrome.storage.local.get(["bridgeToken"]);
      return r.bridgeToken === ${JSON.stringify(SECRET)};
    })()`);
    check("Options also writes the local mirror", savedLocal === true);

    const shown = await evaluate(options, `document.getElementById("status").textContent.trim()`, false);
    check("Save reports an explicit outcome", /saved/i.test(shown), `status=${JSON.stringify(shown)}`);

    // ── 2. The worker must use that exact code ──
    await sleep(3000);
    let worker = (await list()).find((t) => t.type === "service_worker" && t.url.includes(id));
    check("worker is running after save", !!worker);

    const usesIt = await evaluate(worker, `(async () => {
      const r = await chrome.storage.sync.get(["bridgeToken"]);
      return JSON.stringify({ sameCode: r.bridgeToken === ${JSON.stringify(SECRET)}, len: (r.bridgeToken||"").length });
    })()`);
    check("worker reads the same storage area and key", JSON.parse(usesIt).sameCode === true, usesIt);

    const afterSave = JSON.parse(await evaluate(options, `(async () => JSON.stringify(
      await chrome.runtime.sendMessage({ type: "moonwalk_get_status" })))()`));
    check("worker no longer reports 'No pairing code set'",
      !/No pairing code/i.test(afterSave.lastBridgeError || ""),
      `lastBridgeError=${JSON.stringify(afterSave.lastBridgeError)}`);

    // ── 3. Survive a worker restart ──
    // Kill the service worker outright, then let a message cold-start it — the
    // real "after worker restart" case. (chrome.runtime.reload() is not usable
    // here: a CDP-loaded unpacked extension does not come back from it.)
    worker = (await list()).find((t) => t.type === "service_worker" && t.url.includes(id));
    if (worker) {
      await rpc(browserWs, "Target.closeTarget", { targetId: worker.id }).catch(() => {});
    }
    await sleep(2500);
    const stopped = !(await list()).some((t) => t.type === "service_worker" && t.url.includes(id));
    check("service worker was stopped for the restart test", stopped);

    const opts2 = options; // the options page survives; only the worker restarted
    const rawAfterRestart = await evaluate(opts2, `(async () => {
      try {
        return JSON.stringify(await chrome.runtime.sendMessage({ type: "moonwalk_get_status" }));
      } catch (e) { return JSON.stringify({ sendError: String(e && e.message || e) }); }
    })()`);
    let afterRestart;
    try {
      afterRestart = JSON.parse(rawAfterRestart);
    } catch (_) {
      afterRestart = { lastBridgeError: "", raw: rawAfterRestart };
    }
    check("status is retrievable after restart", !afterRestart.raw && !afterRestart.sendError,
      afterRestart.raw || afterRestart.sendError || "");
    const restarted = (await list()).some((t) => t.type === "service_worker" && t.url.includes(id));
    check("the message cold-started the worker again", restarted);
    check("after worker restart the code is still used",
      !/No pairing code/i.test(afterRestart.lastBridgeError || ""),
      `lastBridgeError=${JSON.stringify(afterRestart.lastBridgeError)}`);

    const stillThere = await evaluate(opts2, `(async () => {
      const r = await chrome.storage.sync.get(["bridgeToken"]);
      return r.bridgeToken === ${JSON.stringify(SECRET)};
    })()`);
    check("stored code survived the restart unchanged", stillThere === true);

    // ── 4. Re-saving without retyping must not wipe it ──
    const beforeBlank = await evaluate(opts2, `document.getElementById("bridgeToken").value.length > 0`, false);
    check("Options re-populates the saved code so Save cannot blank it", beforeBlank === true);

    await evaluate(opts2, `(() => { document.getElementById("saveBtn").click(); return true; })()`, false);
    await sleep(1500);
    const survivedResave = await evaluate(opts2, `(async () => {
      const r = await chrome.storage.sync.get(["bridgeToken"]);
      return r.bridgeToken === ${JSON.stringify(SECRET)};
    })()`);
    check("saving again without retyping keeps the code", survivedResave === true);
  } catch (error) {
    console.log("  ERROR:", error.message);
    failed += 1;
  } finally {
    try { browserWs && browserWs.close(); } catch (_) {}
    chrome.kill();
    console.log(failed ? `\n${failed} check(s) failed` : "\nall pairing checks passed");
    process.exit(failed ? 1 : 0);
  }
})();
