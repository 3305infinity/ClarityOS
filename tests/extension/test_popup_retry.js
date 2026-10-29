// Tests the popup against a cold / dead service worker, in a real Chromium DOM.
// Run: npx electron tests/extension/test_popup_retry.js
const path = require("node:path");
const { app, BrowserWindow } = require("electron");

const EXT = path.resolve(__dirname, "..", "..", "chrome_extension");
const PRELOAD = path.join(__dirname, "popup_preload.js");

const CASES = [
  {
    name: "cold worker: first call hangs, retry succeeds",
    scenario: "slow-wake",
    expect: (r) => r.badge === "Connected" && !r.note.includes("did not respond"),
  },
  {
    name: "dead worker: every attempt fails -> Unavailable + reload hint",
    scenario: "dead",
    expect: (r) => r.badge === "Unavailable" && /chrome:\/\/extensions/.test(r.note),
  },
  {
    name: "healthy worker: answers first time",
    scenario: "healthy",
    expect: (r) => r.badge === "Connected" && r.socket.startsWith("Open"),
  },
];

async function run(scenario) {
  const win = new BrowserWindow({
    show: false,
    width: 420,
    height: 760,
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: false,
      nodeIntegration: false,
      additionalArguments: [`--scenario=${scenario}`],
    },
  });
  await win.loadFile(path.join(EXT, "popup.html"));
  // Long enough to cover 3 attempts at a 5s timeout plus the retry gaps.
  await new Promise((r) => setTimeout(r, 20000));
  const result = await win.webContents.executeJavaScript(`({
    badge: document.getElementById("statusLabel").textContent.trim(),
    socket: document.getElementById("socketValue").textContent.trim(),
    note: document.getElementById("activityNote").textContent.trim(),
    attempts: window.__attempts,
    stillLoading: document.body.innerText.includes("Loading…"),
  })`);
  win.destroy();
  return result;
}

// Electron refuses a second loadFile in the same process here, so each case is
// run in its own process (see the runner note in the report).
const ONLY = (process.argv.find((a) => a.startsWith("--case=")) || "").split("=")[1];

app.whenReady().then(async () => {
  let failed = 0;
  for (const c of CASES.filter((c) => !ONLY || c.scenario === ONLY)) {
    const r = await run(c.scenario);
    const ok = c.expect(r) && !r.stillLoading;
    if (!ok) failed += 1;
    console.log(`  ${ok ? "PASS" : "FAIL"}  ${c.name}`);
    console.log(`        badge=${JSON.stringify(r.badge)} socket=${JSON.stringify(r.socket)} attempts=${r.attempts}`);
    console.log(`        note=${JSON.stringify(r.note.slice(0, 110))}`);
  }
  console.log(`\n${CASES.length - failed}/${CASES.length} passed`);
  app.exit(failed ? 1 : 0);
});
