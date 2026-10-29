// Why is the sidebar not clickable? Loads the REAL main.js, then reports the
// window flags that decide whether clicks land, and fires synthetic clicks at
// real control coordinates to see whether handlers run.
const path = require("node:path");
const { app, BrowserWindow, screen } = require("electron");

const ROOT = path.resolve(__dirname, "..", "..");
require(path.join(ROOT, "main.js"));

const line = (k, v) => console.log(`  ${k.padEnd(34)} ${v}`);

app.whenReady().then(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await sleep(25000);   // let main.js finish spawning the Python backend

  const wins = BrowserWindow.getAllWindows();
  const urlOf = (w) => { try { return w.webContents.getURL(); } catch (_) { return ""; } };
  const sidebar = wins.find((w) => urlOf(w).includes("sidebar/index.html"));
  const overlay = wins.find((w) => urlOf(w).includes("renderer/index.html"));

  if (!sidebar) { console.log("NO SIDEBAR WINDOW"); app.exit(1); return; }

  console.log("\n--- window flags ---");
  line("sidebar visible", sidebar.isVisible());
  line("sidebar enabled", sidebar.isEnabled());
  line("sidebar focusable", sidebar.isFocusable());
  line("sidebar focused", sidebar.isFocused());
  line("sidebar alwaysOnTop", sidebar.isAlwaysOnTop());
  line("sidebar bounds", JSON.stringify(sidebar.getBounds()));
  line("overlay visible", overlay ? overlay.isVisible() : "none");
  line("overlay bounds", overlay ? JSON.stringify(overlay.getBounds()) : "none");
  line("overlay alwaysOnTop", overlay ? overlay.isAlwaysOnTop() : "none");

  console.log("\n--- DOM wiring ---");
  const probe = await sidebar.webContents.executeJavaScript(`
    (() => {
      const ids = ["b-send","b-stop","b-screen","b-hide","b-collapse","b-pin","in"];
      const found = Object.fromEntries(ids.map(id => [id, !!document.getElementById(id)]));
      const tabs = document.querySelectorAll(".tab").length;
      const r = document.getElementById("b-send")?.getBoundingClientRect();
      return JSON.stringify({ found, tabs,
        sendRect: r ? {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)} : null,
        bodyPointerEvents: getComputedStyle(document.body).pointerEvents,
        chip: document.getElementById("chip-text")?.textContent.trim(),
        scriptRan: typeof window.__sidebarReady !== "undefined" });
    })()
  `);
  console.log("  " + probe);
  const parsed = JSON.parse(probe);

  // Instrument: does a real click event reach a handler?
  await sidebar.webContents.executeJavaScript(`
    window.__clicks = [];
    document.addEventListener("click", (e) => window.__clicks.push(e.target.id || e.target.className), true);
    true;
  `);

  // Fire a synthetic click at the Send button's centre.
  if (parsed.sendRect && parsed.sendRect.x > 0 && parsed.sendRect.y > 0) {
    const { x, y } = parsed.sendRect;
    sidebar.webContents.sendInputEvent({ type: "mouseDown", x, y, button: "left", clickCount: 1 });
    sidebar.webContents.sendInputEvent({ type: "mouseUp", x, y, button: "left", clickCount: 1 });
    await sleep(600);
  }

  // Type into the textarea and press Enter through the real input pipeline.
  sidebar.webContents.focus();
  await sidebar.webContents.executeJavaScript(`document.getElementById("in").focus(); true`);
  for (const ch of "hello") {
    sidebar.webContents.sendInputEvent({ type: "char", keyCode: ch });
  }
  await sleep(500);
  const typed = await sidebar.webContents.executeJavaScript(
    `JSON.stringify({ value: document.getElementById("in").value, clicks: window.__clicks })`);
  console.log("\n--- interaction ---");
  console.log("  " + typed);

  app.exit(0);
});
