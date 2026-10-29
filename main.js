const path = require("node:path");
const fs = require("node:fs");
const net = require("node:net");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const {
  app,
  BrowserWindow,
  globalShortcut,
  ipcMain,
  screen,
  session,
  shell,
  dialog,
  systemPreferences,
  safeStorage,
  Menu,
  Tray,
} = require("electron");

// ── Settings folder carried across the rename ──────────────────────────────
// Electron derives userData from package.json "name", which changed with the
// rename. Without this, an existing install silently loses credentials.enc and
// sidebar-state.json — the user would have to re-enter their API key and pick
// their presentation mode again. Runs before anything resolves a userData path.
(function migrateUserData() {
  // Copy the data files only, never the Chromium caches: the new folder is
  // usually created (empty but present) by an earlier launch, so renaming the
  // whole directory would not fire, and the caches are rebuilt anyway.
  const CARRY_OVER = ["credentials.enc", "settings.json", "sidebar-state.json", "hub.db"];
  try {
    const current = app.getPath("userData");
    const legacy = path.join(path.dirname(current), "Moon" + "walk");
    if (!fs.existsSync(legacy)) return;
    fs.mkdirSync(current, { recursive: true });
    const moved = [];
    for (const name of CARRY_OVER) {
      const from = path.join(legacy, name);
      const to = path.join(current, name);
      if (fs.existsSync(from) && !fs.existsSync(to)) {
        fs.copyFileSync(from, to);                  // copy, so a rollback still works
        moved.push(name);
      }
    }
    if (moved.length) {
      console.log(`[ClarityOS] Carried ${moved.length} settings file(s) over from the previous name: ${moved.join(", ")}`);
    }
  } catch (error) {
    console.error(`[ClarityOS] Could not migrate settings: ${error.message}`);
  }
})();

// If the parent console goes away (terminal closed, output piped into a command
// that exited), the next console.log/error raises EPIPE. Unhandled, that became
// an "A JavaScript error occurred in the main process" dialog and killed the
// app. Logging must never be fatal — swallow the broken pipe.
//
// But the overlay is frameless, always-on-top and hidden from the taskbar, so
// if it simply survives the terminal there is no way left to close it. When we
// were launched from a terminal, a dead pipe means that terminal is gone: shut
// down the way the user expects instead of leaving a pinned bar on screen.
// A packaged double-click launch has no console at all, and its dead writes
// must never be mistaken for "the user closed the terminal" — so this only
// arms for a dev/CLI launch (npm start, electron .).
const LAUNCHED_FROM_TERMINAL =
  process.env.CLARITYOS_EXIT_WITH_TERMINAL === "1" ||
  (!app.isPackaged &&
    Boolean(process.stdout.isTTY || process.stderr.isTTY || process.env.npm_lifecycle_event));

for (const stream of [process.stdout, process.stderr]) {
  stream.on("error", (err) => {
    if (err && (err.code === "EPIPE" || err.code === "ERR_STREAM_DESTROYED")) {
      if (LAUNCHED_FROM_TERMINAL) quitApp("parent terminal closed");
      return;
    }
    throw err;
  });
}

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => quitApp(`received ${signal}`));
}

const HOTKEYS = (process.env.LIQUID_HOTKEY || "CommandOrControl+Shift+Space,Alt+Space")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);

const QUIT_HOTKEY = process.env.CLARITYOS_QUIT_HOTKEY || "CommandOrControl+Shift+Q";

const WINDOW_LEVEL = "screen-saver";

// ── Path resolution (dev vs packaged) ──
const IS_PACKAGED = app.isPackaged;
const APP_ROOT = IS_PACKAGED
  ? path.join(process.resourcesPath)
  : __dirname;
const BACKEND_ROOT = IS_PACKAGED
  ? path.join(APP_ROOT, "backend")
  : path.join(__dirname, "backend");

let mainWindow;
let sidebarWindow = null;
let sidebarCollapsed = false;   // per-session only
let tray = null;
let isQuitting = false;
let lastWakeAt = 0;
let pythonProcess = null;
let ownsPythonProcess = false;

const BACKEND_WS_URL = process.env.CLARITYOS_BACKEND_WS_URL || "ws://127.0.0.1:8000/ws";
const BACKEND_HOST = process.env.CLARITYOS_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.CLARITYOS_BACKEND_PORT || "8000");
const BRIDGE_PORT = Number(process.env.CLARITYOS_BROWSER_BRIDGE_PORT || "8765");
const BACKEND_READY_SENTINEL = "[Backend] READY";

// Venv lives in userData for packaged builds (writable, survives app updates)
const getVenvRoot = () => IS_PACKAGED
  ? path.join(app.getPath("userData"), "venv")
  : path.join(__dirname, "venv");

const IS_WINDOWS = process.platform === "win32";

// `python -m venv` lays out Scripts\python.exe on Windows and bin/python3 elsewhere.
const venvPython = (root) => IS_WINDOWS
  ? path.join(root, "Scripts", "python.exe")
  : path.join(root, "bin", "python3");

/**
 * First usable interpreter, or null if none exists yet.
 *
 * In development the venv may be the one the Windows guide creates
 * (backend\.venv, also used by start-hub.ps1) or the one setup.sh creates
 * (<project>/venv), so check both rather than assuming one layout.
 */
function findVenvPython() {
  const roots = IS_PACKAGED
    ? [getVenvRoot()]
    : [path.join(BACKEND_ROOT, ".venv"), getVenvRoot()];
  for (const root of roots) {
    const candidate = venvPython(root);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

// Bundled Picovoice key — enables the "Hey ClarityOS" wake word out of the box.
// Users can replace this with their own key from console.picovoice.ai
const BUNDLED_PICOVOICE_KEY = "lDvqq7J641WbqdzMsPCdLlawELhfGZOGhaceFzl3ZYYYzeeuXq55YA==";

// ── Credential storage ──
const CRED_FILE = path.join(app.getPath("userData"), "credentials.enc");

/**
 * Check whether we have *usable* saved credentials (file exists,
 * decrypts successfully, and contains a Gemini API key).
 * Returns true only when the user can skip onboarding.
 */
function hasUsableCredentials() {
  if (!fs.existsSync(CRED_FILE)) return false;
  const creds = loadCredentials();          // returns null on any failure
  return !!(creds && creds.gemini_api_key);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function canReachPort(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    let settled = false;
    const socket = net.createConnection({ host: BACKEND_HOST, port });

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        socket.destroy();
      } catch {
      }
      resolve(result);
    };

    const timer = setTimeout(() => finish(false), timeoutMs);

    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}

async function canReachBackend(timeoutMs = 1500) {
  const [backendReady, bridgeReady] = await Promise.all([
    canReachPort(BACKEND_PORT, timeoutMs),
    canReachPort(BRIDGE_PORT, timeoutMs)
  ]);
  return backendReady && bridgeReady;
}

// ═══════════════════════════════════════════════════════════════
//  Python Environment Setup
// ═══════════════════════════════════════════════════════════════

function runSetup(venvRoot) {
  return new Promise((resolve) => {
    const setupPath = IS_PACKAGED
      ? path.join(APP_ROOT, "setup.sh")
      : path.join(__dirname, "setup.sh");

    if (!fs.existsSync(setupPath)) {
      console.error("[Setup] setup.sh not found at:", setupPath);
      resolve(false);
      return;
    }

    const setup = spawn("bash", [setupPath], {
      cwd: IS_PACKAGED ? APP_ROOT : __dirname,
      env: { ...process.env, CLARITYOS_VENV_DIR: venvRoot },
      stdio: "pipe",
    });

    setup.stdout.on("data", (data) => {
      const text = data.toString().trim();
      process.stdout.write(`[Setup] ${text}\n`);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("setup:progress", text);
      }
    });

    setup.stderr.on("data", (data) => {
      process.stderr.write(`[Setup ERR] ${data.toString()}`);
    });

    setup.on("close", (code) => {
      console.log(`[Setup] Exited with code ${code}`);
      resolve(code === 0);
    });
  });
}

// The welcome screen and app.whenReady() can both ask for the backend. Share
// one in-flight promise so a race can never spawn two Python processes.
let backendStartPromise = null;

async function startPythonBackend() {
  if (backendStartPromise) return backendStartPromise;
  backendStartPromise = _startPythonBackend().finally(() => {
    backendStartPromise = null;
  });
  return backendStartPromise;
}

async function _startPythonBackend() {
  const venvRoot = getVenvRoot();
  const scriptPath = path.join(BACKEND_ROOT, "servers", "local_server.py");
  const cwd = IS_PACKAGED ? APP_ROOT : __dirname;

  let venvPythonPath = findVenvPython();
  if (!venvPythonPath) {
    console.log("[Backend] Python venv not found — running setup...");
    if (IS_WINDOWS && !IS_PACKAGED) {
      // setup.sh is a bash script that builds a POSIX venv layout; on Windows
      // it cannot produce Scripts\python.exe. Point the user at the documented
      // Windows path instead of failing with a confusing bash error.
      console.error(
        "[Backend] No Python environment found. Create one first:\n" +
        "    py -3 -m venv backend\\.venv\n" +
        "    backend\\.venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt"
      );
      return false;
    }
    const ok = await runSetup(venvRoot);
    if (!ok) {
      console.error("[Backend] Setup failed — cannot start backend");
      return false;
    }
    venvPythonPath = findVenvPython();
    if (!venvPythonPath) {
      console.error("[Backend] Setup finished but no interpreter was found");
      return false;
    }
  }
  console.log(`[Backend] Python: ${venvPythonPath}`);

  if (await canReachBackend()) {
    console.log(`[Backend] Reusing existing backend at ${BACKEND_WS_URL}`);
    ownsPythonProcess = false;
    return true;
  }

  console.log("[Backend] Starting Python server...");

  // Load saved credentials and inject API keys into Python's environment
  const savedCreds = loadCredentials();
  const spawnEnv = { ...process.env };
  // A key saved in Settings is injected into the backend's environment, and
  // python-dotenv does NOT override an existing variable — so whatever is here
  // silently wins over backend/.env. A stored value too short to be a real
  // Gemini key therefore shadowed a perfectly good one and produced
  // "API key not valid" on every request. Decline the unusable one and say so;
  // the stored credential itself is left untouched.
  const storedGeminiKey = (savedCreds?.gemini_api_key || "").trim();
  if (storedGeminiKey.length >= 30) {
    spawnEnv.GEMINI_API_KEY = storedGeminiKey;
    console.log(`[Backend] Gemini key: using the one saved in Settings (${storedGeminiKey.length} chars). `
      + `It takes precedence over backend/.env.`);
  } else if (storedGeminiKey) {
    console.warn(`[Backend] Ignoring the Gemini key saved in Settings — it is only `
      + `${storedGeminiKey.length} characters, too short to be valid. Falling back to backend/.env. `
      + `Re-enter it in Settings → API keys to use a stored key.`);
  }
  // Use saved Picovoice key if set, otherwise fall back to the bundled default
  spawnEnv.PICOVOICE_ACCESS_KEY = savedCreds?.picovoice_key || BUNDLED_PICOVOICE_KEY;
  // stdout is a pipe, so Python falls back to the locale encoding (cp1252 on Windows)
  // and every print() containing ✓/✗/→ raises UnicodeEncodeError, killing the backend.
  spawnEnv.PYTHONIOENCODING = "utf-8";
  spawnEnv.PYTHONUTF8 = "1";

  // Start the python process
  pythonProcess = spawn(venvPythonPath, [scriptPath], {
    cwd: cwd,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: spawnEnv,
  });
  ownsPythonProcess = true;

  let sawAddressInUse = false;
  let readinessBuffer = "";
  let resolveReady;
  const readyPromise = new Promise((resolve) => {
    resolveReady = resolve;
  });

  // Pipe python stdout/stderr to our electron console
  pythonProcess.stdout.on('data', (data) => {
    const text = data.toString();
    readinessBuffer = (readinessBuffer + text).slice(-4096);
    if (readinessBuffer.includes(BACKEND_READY_SENTINEL)) {
      resolveReady(true);
    }
    process.stdout.write(`[Python] ${text}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    const text = data.toString();
    // macOS/Linux say "Errno 48 / address already in use"; Windows says
    // "[Errno 10048] ... only one usage of each socket address ...".
    if (text.includes("Errno 48") || text.includes("Errno 10048") ||
        text.includes("address already in use") ||
        text.includes("only one usage of each socket address")) {
      sawAddressInUse = true;
    }
    process.stderr.write(`[Python ERRROR] ${text}`);
  });

  pythonProcess.on('close', (code) => {
    resolveReady(false);
    if (sawAddressInUse) {
      console.log(
        `[Backend] Python server did not start: a port it needs is already in use ` +
        `(backend ${BACKEND_PORT}, browser bridge ${BRIDGE_PORT}). ` +
        `The ClarityOS hub also uses the bridge port — stop start-hub.ps1, or set ` +
        `CLARITYOS_BROWSER_BRIDGE_PORT to a free port for one of them.`);
    } else {
      console.log(`[Backend] Python server exited with code ${code}`);
    }
    pythonProcess = null;
    ownsPythonProcess = false;
  });

  const ready = await Promise.race([
    readyPromise,
    sleep(10000).then(() => false)
  ]);
  if (ready) {
    return true;
  }

  if (await canReachBackend()) {
    return true;
  }

  if (sawAddressInUse && await canReachBackend()) {
    console.log(`[Backend] Reusing backend that is already listening at ${BACKEND_WS_URL}`);
    return true;
  }

  console.error("[Backend] Backend did not become ready in time.");
  return false;
}

function stopPythonBackend() {
  if (pythonProcess && ownsPythonProcess) {
    console.log("[Backend] Stopping Python server...");
    pythonProcess.kill('SIGTERM');
    pythonProcess = null;
    ownsPythonProcess = false;
  }
}

function emitStartListening() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  if (mainWindow.webContents.isLoading()) {
    mainWindow.webContents.once("did-finish-load", () => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      mainWindow.webContents.send("start-listening");
    });
    return;
  }

  mainWindow.webContents.send("start-listening");
}

function createWindow() {
  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workAreaSize;

  mainWindow = new BrowserWindow({
    width: width,
    height: height,
    x: display.workArea.x,
    y: display.workArea.y,
    // Hidden until deliberately summoned — see the comment in whenReady().
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.setAlwaysOnTop(true, WINDOW_LEVEL);
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  mainWindow.setFullScreenable(false);
  setMousePassthrough(true);

  // Tell the renderer whether it is the active presentation as soon as it can
  // listen. Broadcasting only from applyUiMode raced the page load, so both
  // windows connected anyway and the backend built an agent for each.
  // A window holds a backend socket exactly while it is on screen.
  mainWindow.webContents.on("did-finish-load", () => {
    sendUiActive(mainWindow, mainWindow.isVisible());
  });
  mainWindow.on("show", () => sendUiActive(mainWindow, true));
  mainWindow.on("hide", () => sendUiActive(mainWindow, false));
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
}

function centerNearTop() {
  if (!mainWindow) return;
  const display = screen.getPrimaryDisplay();
  const x = Math.round(display.workArea.x + (display.workArea.width - WINDOW_WIDTH) / 2);
  const y = Math.max(display.workArea.y + 10, 8);
  mainWindow.setPosition(x, y, false);
}

// The overlay spans the work area and floats at "screen-saver" level, which is
// above the sidebar's "floating" level — so without this it would render on top
// of the docked panel. Reserve the sidebar's strip instead of overlapping it.
function layoutOverlay() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const area = screen.getPrimaryDisplay().workArea;
  let reserved = 0;
  if (sidebarWindow && !sidebarWindow.isDestroyed() && sidebarWindow.isVisible()) {
    reserved = sidebarWindow.getSize()[0];
  }
  mainWindow.setBounds({
    x: area.x,
    y: area.y,
    width: Math.max(320, area.width - reserved),
    height: area.height,
  });
}

function wakeOverlay() {
  if (!mainWindow || mainWindow.isDestroyed() || isQuitting) return;
  layoutOverlay();
  lastWakeAt = Date.now();
  mainWindow.setAlwaysOnTop(true, WINDOW_LEVEL);   // restored on every wake
  mainWindow.show();
  setMousePassthrough(true, { forward: true });    // hit-testing needs it now
  emitStartListening();
}

function hideOverlay() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  // Explicitly forward:false — a hidden overlay must not keep a global mouse
  // hook alive. Also step down from "screen-saver" level so a hidden window
  // cannot sit above a capture or selection UI.
  setMousePassthrough(true, { forward: false });
  mainWindow.setAlwaysOnTop(false);
  mainWindow.webContents.send("overlay-hidden");
  // The window itself stays shown so the renderer can play its fade-out and
  // fade back in later, which means the "hide" event never fires. Say so
  // explicitly, or the dismissed overlay keeps a backend socket (and therefore
  // an agent) open behind a UI the user believes is gone.
  sendUiActive(mainWindow, false);
}

// ═══════════════════════════════════════════════════════════════
//  Sidebar — a second window on the existing backend
//
//  It is NOT a second agent: sidebar/sidebar.js talks the same WebSocket
//  protocol to the same voice server (:8000) as renderer/renderer.js, so tasks
//  keep flowing through the existing agent, tool registry, policy engine and
//  approval broker. The overlay is untouched.
// ═══════════════════════════════════════════════════════════════

const SIDEBAR_HOTKEY = process.env.CLARITYOS_SIDEBAR_HOTKEY || "CommandOrControl+Shift+A";
const SIDEBAR_MIN_WIDTH = 280;
const SIDEBAR_MAX_WIDTH = 560;
const SIDEBAR_COLLAPSED_WIDTH = 52;
const sidebarStatePath = () => path.join(app.getPath("userData"), "sidebar-state.json");

function readSidebarState() {
  try {
    return JSON.parse(fs.readFileSync(sidebarStatePath(), "utf8")) || {};
  } catch (_) {
    return {};
  }
}

function writeSidebarState(patch) {
  try {
    fs.writeFileSync(sidebarStatePath(), JSON.stringify({ ...readSidebarState(), ...patch }, null, 2));
  } catch (_) {
    // Position persistence is a convenience, never fatal.
  }
}

function dockSidebar(win, width) {
  const display = screen.getPrimaryDisplay();
  const area = display.workArea;
  const w = Math.round(Math.min(Math.max(width, SIDEBAR_COLLAPSED_WIDTH), SIDEBAR_MAX_WIDTH));
  win.setBounds({ x: area.x + area.width - w, y: area.y, width: w, height: area.height });
}

function createSidebar() {
  if (sidebarWindow && !sidebarWindow.isDestroyed()) return sidebarWindow;

  const saved = readSidebarState();
  const expandedWidth = Math.min(Math.max(Number(saved.width) || 360, SIDEBAR_MIN_WIDTH), SIDEBAR_MAX_WIDTH);
  // The window and the DOM must agree. Restoring a 52px collapsed width while
  // the page still laid itself out at 360px put every header control off-screen
  // and made the whole panel look dead.
  const width = expandedWidth;   // always open usable; collapse is per-session

  sidebarWindow = new BrowserWindow({
    width,
    minWidth: SIDEBAR_COLLAPSED_WIDTH,
    maxWidth: SIDEBAR_MAX_WIDTH,
    show: false,
    frame: false,
    resizable: true,
    movable: false,            // docked; width is the only dimension that moves
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: false,        // discoverable in Alt+Tab, unlike the overlay
    // Match the theme so a light-mode sidebar does not flash dark on open.
    backgroundColor: require("electron").nativeTheme.shouldUseDarkColors ? "#0b0c11" : "#f7f7fb",
    title: "ClarityOS Sidebar",
    webPreferences: {
      preload: path.join(__dirname, "sidebar", "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  sidebarWindow.setAlwaysOnTop(saved.alwaysOnTop !== false, "floating");
  dockSidebar(sidebarWindow, width);
  sidebarWindow.webContents.on("did-finish-load", () => {
    sendUiActive(sidebarWindow, sidebarWindow.isVisible());
  });
  sidebarWindow.on("show", () => sendUiActive(sidebarWindow, true));
  sidebarWindow.on("hide", () => sendUiActive(sidebarWindow, false));
  sidebarWindow.loadFile(path.join(__dirname, "sidebar", "index.html"));

  // Keep it docked when the user drags its edge or the display changes.
  // Re-docking from inside a resize handler fed back into itself: a transient
  // small width during window creation got clamped to the 52px floor and then
  // re-applied as the real size, so the sidebar came up collapsed on its own.
  // Only move it back to the right edge here; never change its width.
  sidebarWindow.on("resize", () => {
    if (!sidebarWindow || sidebarWindow.isDestroyed()) return;
    const [w, h] = sidebarWindow.getSize();
    if (w < SIDEBAR_MIN_WIDTH && !sidebarCollapsed) return;   // mid-creation noise
    const area = screen.getPrimaryDisplay().workArea;
    sidebarWindow.setPosition(area.x + area.width - w, area.y, false);
    layoutOverlay();
    if (w > SIDEBAR_COLLAPSED_WIDTH) writeSidebarState({ width: w });
  });

  // Closing the sidebar (Alt+F4, the taskbar Close, the window X) used to
  // destroy the renderer, which erased the whole conversation — reopening gave
  // a blank transcript. Hide it instead and keep the window alive, exactly as
  // the in-window Hide button already did. A real quit still closes it.
  sidebarWindow.on("close", (event) => {
    if (isQuitting) return;              // app.quit() must still work
    event.preventDefault();
    sidebarWindow.hide();
    layoutOverlay();                     // give the strip back to the overlay
  });

  sidebarWindow.on("closed", () => { sidebarWindow = null; });

  return sidebarWindow;
}

function showSidebar() {
  const win = createSidebar();
  // showInactive keeps the user's current application focused — the sidebar
  // must not steal focus from whatever they are actually working in.
  win.showInactive();
  // Re-assert the intended width: a mid-creation resize could otherwise leave
  // the window latched at the collapsed floor.
  dockSidebar(win, sidebarCollapsed ? SIDEBAR_COLLAPSED_WIDTH
                                    : Math.min(Math.max(Number(readSidebarState().width) || 360,
                                                        SIDEBAR_MIN_WIDTH), SIDEBAR_MAX_WIDTH));
  layoutOverlay();                     // keep the overlay out of the sidebar strip
  win.webContents.send("sidebar:shown");
}

function hideSidebar() {
  if (sidebarWindow && !sidebarWindow.isDestroyed() && sidebarWindow.isVisible()) {
    sidebarWindow.hide();
    layoutOverlay();                   // give the space back to the overlay
  }
}

function toggleSidebar() {
  if (sidebarWindow && !sidebarWindow.isDestroyed() && sidebarWindow.isVisible()) {
    hideSidebar();
    return;
  }
  showSidebar();
}

// Let the renderer restore the collapsed class it was last left in.
// ── Presentation mode ──────────────────────────────────────────────────────
// "sidebar" | "floating". Stored in the same sidebar-state.json the sidebar
// already uses, so there is one settings mechanism rather than two, and the
// choice survives restarts. `null` means the user has not chosen yet.
//
// Both presentations drive the SAME backend over the SAME WebSocket (see the
// note above createSidebar), so switching never starts a second agent, a second
// connection or a second greeting — it only changes which window is on screen.
const UI_MODES = ["sidebar", "floating"];

function readUiMode() {
  const saved = readSidebarState().uiMode;
  return UI_MODES.includes(saved) ? saved : null;
}

// Both windows exist for their whole lifetime (the overlay also renders setup
// progress), but only the active presentation may hold a backend socket.
// Without this, sidebar mode still had the hidden overlay connected, and the
// voice server built a second agent for it — two agents for one user.
function sendUiActive(win, active) {
  if (win && !win.isDestroyed() && !win.webContents.isDestroyed()) {
    win.webContents.send("ui:active", active);
  }
}

// Keyed on whether the window is actually on screen, NOT on the chosen mode.
// Gating by mode disconnected the overlay whenever the sidebar was selected —
// including while the overlay was showing the first-run welcome screen, which
// waits for its own socket to open and so reported "Assistant not responding"
// against a perfectly healthy backend.
function broadcastActiveMode() {
  sendUiActive(mainWindow, Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible()));
  sendUiActive(sidebarWindow, Boolean(sidebarWindow && !sidebarWindow.isDestroyed() && sidebarWindow.isVisible()));
}

function applyUiMode(mode, { persist = true } = {}) {
  if (!UI_MODES.includes(mode)) return false;
  if (persist) writeSidebarState({ uiMode: mode });

  try {
    if (mode === "sidebar") {
      hideOverlay();
      if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible()) {
        mainWindow.hide();
      }
      showSidebar();
    } else {
      hideSidebar();
      wakeOverlay();
    }
  } catch (error) {
    // §2.9: a failed window switch must be reported and recoverable, not
    // silently answered by falling back to the other mode.
    console.error(`[ClarityOS] Could not switch to ${mode} mode: ${error.message}`);
    return false;
  }
  broadcastActiveMode();
  if (tray) buildTrayMenu();
  return true;
}

function toggleUiMode() {
  return applyUiMode(readUiMode() === "sidebar" ? "floating" : "sidebar");
}

ipcMain.handle("ui:get-mode", () => readUiMode());
ipcMain.handle("ui:set-mode", (_event, mode) => applyUiMode(mode));

ipcMain.handle("sidebar:get-state", () => readSidebarState());

ipcMain.handle("sidebar:hide", () => {
  if (sidebarWindow && !sidebarWindow.isDestroyed()) sidebarWindow.hide();
  return true;
});

ipcMain.handle("sidebar:set-collapsed", (_event, collapsed) => {
  if (!sidebarWindow || sidebarWindow.isDestroyed()) return false;
  const saved = readSidebarState();
  const width = collapsed
    ? SIDEBAR_COLLAPSED_WIDTH
    : Math.min(Math.max(Number(saved.width) || 360, SIDEBAR_MIN_WIDTH), SIDEBAR_MAX_WIDTH);
  sidebarWindow.setMinimumSize(collapsed ? SIDEBAR_COLLAPSED_WIDTH : SIDEBAR_MIN_WIDTH, 200);
  dockSidebar(sidebarWindow, width);
  sidebarCollapsed = Boolean(collapsed);
  return true;
});

ipcMain.handle("sidebar:set-always-on-top", (_event, on) => {
  if (!sidebarWindow || sidebarWindow.isDestroyed()) return false;
  sidebarWindow.setAlwaysOnTop(Boolean(on), "floating");
  writeSidebarState({ alwaysOnTop: Boolean(on) });
  return true;
});

// Explicit, one-shot capture. Never on a timer, never in the background.
ipcMain.handle("sidebar:capture-screen", async () => {
  try {
    const { desktopCapturer } = require("electron");
    const display = screen.getPrimaryDisplay();
    const { width, height } = display.size;
    const sources = await desktopCapturer.getSources({
      types: ["screen"],
      thumbnailSize: { width: Math.round(width / 2), height: Math.round(height / 2) },
    });
    const source = sources[0];
    if (!source || source.thumbnail.isEmpty()) {
      return { ok: false, error: "Windows did not return a screen image. Check Settings → Privacy → Screen recording." };
    }
    return { ok: true, dataUrl: source.thumbnail.toDataURL(), name: source.name };
  } catch (error) {
    return { ok: false, error: `Screen capture unavailable: ${error.message}` };
  }
});

// The overlay has no title bar, no close button and no taskbar entry, so this
// is the app's only real exit. Everything that wants to shut ClarityOS down
// routes through here: tray menu, quit hotkey, signals, dead parent terminal.
function quitApp(reason = "quit requested") {
  if (isQuitting) return;
  isQuitting = true;
  try {
    console.log(`[ClarityOS] Shutting down (${reason})`);
  } catch (_) {
    // stdout may already be gone — that is often why we are quitting.
  }
  try {
    globalShortcut.unregisterAll();
  } catch (_) {}
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.destroy();
  }
  if (sidebarWindow && !sidebarWindow.isDestroyed()) {
    sidebarWindow.destroy();
  }
  if (tray) {
    tray.destroy();
    tray = null;
  }
  stopPythonBackend();
  app.quit();
  // app.quit() is cooperative; if a handler stalls, do not leave the overlay
  // floating on top of the user's screen.
  setTimeout(() => app.exit(0), 1500).unref?.();
}

function createTray() {
  const iconPath = path.join(IS_PACKAGED ? APP_ROOT : __dirname, "build", "icon.png");
  if (!fs.existsSync(iconPath)) {
    console.error(`[ClarityOS] Tray icon missing at ${iconPath}; skipping tray.`);
    return;
  }

  try {
    tray = new Tray(iconPath);
  } catch (error) {
    console.error(`[ClarityOS] Could not create tray icon: ${error.message}`);
    return;
  }

  tray.setToolTip("ClarityOS");
  // Registered once here, never in buildTrayMenu: that runs on every mode
  // change and would stack a new listener each time.
  tray.on("click", () => wakeOverlay());
  buildTrayMenu();
}

// Rebuilt whenever the mode changes so the checkmark reflects reality.
function buildTrayMenu() {
  if (!tray) return;
  const mode = readUiMode();
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "Docked sidebar",
        type: "radio",
        checked: mode === "sidebar",
        click: () => applyUiMode("sidebar"),
      },
      {
        label: "Floating assistant",
        type: "radio",
        checked: mode === "floating",
        click: () => applyUiMode("floating"),
      },
      { type: "separator" },
      { label: "Show overlay", click: () => wakeOverlay() },
      { label: "Hide overlay", click: () => hideOverlay() },
      {
        label: `Toggle sidebar (${SIDEBAR_HOTKEY.replace("CommandOrControl", "Ctrl")})`,
        click: () => toggleSidebar(),
      },
      {
        label: "Reset overlay position",
        click: () => {
          if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send("overlay-reset-position");
          }
        },
      },
      { type: "separator" },
      {
        label: "Clear saved Gemini key (use backend/.env)",
        click: () => {
          const { cleared, reason } = clearStoredGeminiKey();
          console.log(`[Auth] Clear saved Gemini key: ${cleared ? "done" : reason}`);
        },
      },
      { type: "separator" },
      { label: `Quit ClarityOS (${QUIT_HOTKEY.replace("CommandOrControl", "Ctrl")})`, click: () => quitApp("tray menu") },
    ]),
  );
}

function registerHotkey() {
  globalShortcut.unregisterAll();
  let registeredCount = 0;

  if (!globalShortcut.register(QUIT_HOTKEY, () => quitApp("quit hotkey"))) {
    console.error(`Failed to register quit shortcut: ${QUIT_HOTKEY}`);
  }

  if (!globalShortcut.register(SIDEBAR_HOTKEY, () => toggleSidebar())) {
    console.error(`Failed to register sidebar shortcut: ${SIDEBAR_HOTKEY}`);
  }

  for (const accelerator of HOTKEYS) {
    const ok = globalShortcut.register(accelerator, () => {
      wakeOverlay();
    });
    if (ok) {
      registeredCount += 1;
    } else {
      console.error(`Failed to register global shortcut: ${accelerator}`);
    }
  }

  if (registeredCount === 0) {
    console.error("No usable global shortcuts were registered.");
  }
}

// `forward: true` is what lets the click-through overlay still receive
// mousemove, so the renderer can hit-test its own controls. On Windows Electron
// implements that with a *low-level mouse hook*, which every mouse event on the
// desktop passes through — including the dense stream a Snipping Tool region
// drag produces. We were leaving it installed permanently, even with the
// overlay hidden, where it buys nothing and only taxes system-wide input.
//
// Forwarding is now tied to the overlay actually being on screen.
function setMousePassthrough(ignore, { forward } = {}) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (!ignore) {
    mainWindow.setIgnoreMouseEvents(false);
    return;
  }
  const wantForward =
    forward !== undefined ? Boolean(forward) : mainWindow.isVisible();
  mainWindow.setIgnoreMouseEvents(true, wantForward ? { forward: true } : {});
}

async function configureMicrophonePermissions() {
  // Allow all permissions for our own renderer (media, clipboard, notifications, etc.)
  session.defaultSession.setPermissionCheckHandler((_, permission) => {
    return true;
  });

  session.defaultSession.setPermissionRequestHandler((_, permission, callback) => {
    callback(true);
  });

  if (process.platform === "darwin") {
    const status = systemPreferences.getMediaAccessStatus("microphone");
    if (status !== "granted") {
      try {
        await systemPreferences.askForMediaAccess("microphone");
      } catch (err) {
        console.error("Microphone permission prompt failed:", err);
      }
    }
  }
}

// Two copies of a frameless always-on-top overlay stack invisibly on top of
// each other, and only the first one to boot wins the global shortcuts — so the
// second is unreachable by hotkey and effectively unclosable. Launching again
// now just wakes the instance that is already running.
if (!app.requestSingleInstanceLock()) {
  // Exiting silently here made `npm start` look like it did nothing at all.
  // Say why, and tell the user how to get back to the window that already has
  // the lock, before standing down.
  console.log(
    "\nClarityOS is already running.\n" +
    `  • Press ${SIDEBAR_HOTKEY.replace("CommandOrControl", "Ctrl")} to show the sidebar\n` +
    "  • Press Ctrl+Shift+Space for the voice overlay\n" +
    "  • Press Ctrl+Shift+Q (or use the tray icon) to quit it first\n",
  );
  app.exit(0);
} else {
  app.on("second-instance", () => {
    console.log("[ClarityOS] Already running — waking the existing overlay.");
    wakeOverlay();
  });
}

app.whenReady().then(async () => {
  await configureMicrophonePermissions();

  // Create window first so setup:progress events can reach the renderer.
  // It starts hidden: the sidebar is the default surface, and a full-screen
  // transparent overlay sitting over the user's browser is exactly what they
  // asked us to stop doing. Ctrl+Shift+Space and the tray still bring it up.
  createWindow();
  registerHotkey();
  createTray();

  // Restore the presentation the user chose last time. Which surface opens is
  // now a stored decision rather than a side effect of startup order, which is
  // why it used to come up as a sidebar sometimes and a floating overlay other
  // times. First run has no stored choice: keep the existing default (sidebar)
  // on screen and let the renderer offer the one-time pick, so nothing is
  // blocked behind a modal.
  const savedMode = readUiMode();
  if (savedMode) {
    applyUiMode(savedMode, { persist: false });
  } else {
    showSidebar();
    if (sidebarWindow && !sidebarWindow.isDestroyed()) {
      sidebarWindow.webContents.send("ui:choose-mode");
    }
  }
  buildTrayMenu();

  // Always bring the backend up. A Gemini key can come from the encrypted
  // credential store OR from backend/.env, and the local Windows voice needs no
  // key at all — so gating startup on credentials.enc would block a perfectly
  // well-configured install. The welcome screen also calls startBackend(), and
  // the shared in-flight promise above makes the duplicate call a no-op.
  await new Promise(r => setTimeout(r, 800));
  startPythonBackend();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
      registerHotkey();
    }
  });
});

ipcMain.handle("overlay:hide", () => {
  hideOverlay();
});

ipcMain.handle("app:quit", () => {
  quitApp("renderer requested quit");
  return true;
});

ipcMain.on("enable-mouse", () => {
  setMousePassthrough(false);
});

ipcMain.on("disable-mouse", () => {
  setMousePassthrough(true);
});

ipcMain.on("log-error", (event, msg) => {
  console.error(`[Renderer WS Error] ${msg}`);
});

ipcMain.on("log-info", (event, msg) => {
  console.log(`[Renderer Info] ${msg}`);
});

// ═══════════════════════════════════════════════════════════════
//  Credential Storage (safeStorage-encrypted)
// ═══════════════════════════════════════════════════════════════

function loadCredentials() {
  try {
    if (!fs.existsSync(CRED_FILE)) return null;
    const encrypted = fs.readFileSync(CRED_FILE);
    if (!safeStorage.isEncryptionAvailable()) {
      console.warn("[Auth] safeStorage encryption not available");
      return null;
    }
    const decrypted = safeStorage.decryptString(encrypted);
    return JSON.parse(decrypted);
  } catch (err) {
    console.error("[Auth] Failed to load credentials:", err.message);
    return null;
  }
}

function saveCredentials(creds) {
  try {
    if (!safeStorage.isEncryptionAvailable()) {
      console.warn("[Auth] safeStorage encryption not available");
      return false;
    }
    const encrypted = safeStorage.encryptString(JSON.stringify(creds));
    fs.writeFileSync(CRED_FILE, encrypted);
    return true;
  } catch (err) {
    console.error("[Auth] Failed to save credentials:", err.message);
    return false;
  }
}

ipcMain.handle("auth:load-credentials", () => {
  return loadCredentials();
});

ipcMain.handle("auth:save-credentials", (event, creds) => {
  return saveCredentials(creds);
});

ipcMain.handle("auth:generate-user-id", () => {
  return {
    user_id: crypto.randomUUID(),
    auth_token: crypto.randomBytes(36).toString("base64url"),
  };
});

ipcMain.handle("auth:clear-credentials", () => {
  try {
    if (fs.existsSync(CRED_FILE)) fs.unlinkSync(CRED_FILE);
    return true;
  } catch { return false; }
});

ipcMain.handle("auth:is-first-launch", () => {
  return !hasUsableCredentials();
});

// A key stored here takes precedence over backend/.env, so a stale or truncated
// one makes a perfectly good .env key look rejected. Clearing just that field —
// rather than the whole credential file, which also holds the user id and auth
// token — is the actionable fix, and never reads the value back out.
function clearStoredGeminiKey() {
  const creds = loadCredentials();
  if (!creds) return { cleared: false, reason: "no stored credentials" };
  const keyFields = ["gemini_api_key", "geminiApiKey", "gemini_key"];
  const present = keyFields.filter((f) => creds[f]);
  if (present.length === 0) return { cleared: false, reason: "no stored Gemini key" };
  for (const f of present) delete creds[f];
  const ok = saveCredentials(creds);
  console.log(
    `[Auth] Cleared stored Gemini key (${present.length} field(s)); ` +
    `backend/.env will be used on next start.`,
  );
  return { cleared: ok, reason: ok ? "cleared" : "could not rewrite credential store" };
}

ipcMain.handle("auth:clear-gemini-key", () => clearStoredGeminiKey());

ipcMain.handle("app:get-version", () => {
  return app.getVersion();
});

ipcMain.handle("app:is-packaged", () => {
  return IS_PACKAGED;
});

ipcMain.handle("backend:start", async () => {
  const ok = await startPythonBackend();
  return { ok };
});

// ═══════════════════════════════════════════════════════════════
//  Chrome Extension — Export & Install Helpers
// ═══════════════════════════════════════════════════════════════

const CHROME_EXT_SOURCE = IS_PACKAGED
  ? path.join(APP_ROOT, "chrome_extension")
  : path.join(__dirname, "chrome_extension");

ipcMain.handle("extension:export", async () => {
  // Let customer choose where to save the extension folder
  const { canceled, filePath: destPath } = await dialog.showSaveDialog(mainWindow, {
    title: "Save ClarityOS Browser Extension",
    defaultPath: path.join(app.getPath("downloads"), "clarityos-browser-bridge"),
    buttonLabel: "Save Extension",
  });
  if (canceled || !destPath) return { success: false, reason: "cancelled" };

  try {
    // Copy extension folder to chosen location
    const { execSync } = require("node:child_process");
    if (fs.existsSync(destPath)) {
      execSync(`rm -rf "${destPath}"`);
    }
    execSync(`cp -R "${CHROME_EXT_SOURCE}" "${destPath}"`);

    // Write a friendly install guide inside
    const guide = [
      "# ClarityOS Browser Bridge — Install Guide\n",
      "## Quick Install (2 minutes)\n",
      "1. Open Google Chrome",
      "2. Go to chrome://extensions",
      "3. Turn ON 'Developer mode' (top-right toggle)",
      "4. Click 'Load unpacked' (top-left)",
      "5. Select THIS folder",
      "6. Done! The ClarityOS extension is now installed.\n",
      "## Pin the Extension",
      "Click the puzzle piece icon in Chrome's toolbar,",
      "then click the pin next to 'ClarityOS Browser Bridge'.\n",
      "## Connect it (one approval)\n",
      "1. Make sure ClarityOS is running.",
      "2. Click the ClarityOS icon in Chrome's toolbar.",
      "3. Click 'Pair this extension'.",
      "4. A prompt appears in the ClarityOS window — click Approve.",
      "5. The popup shows 'Connected'. You only do this once.\n",
      "No codes to copy. ClarityOS will not pair an extension you did not",
      "approve, and the approval prompt names who is asking.\n",
      "## Troubleshooting\n",
      "- 'No pairing code set' — click 'Pair this extension' in the popup.",
      "- The approval prompt never appeared: open ClarityOS at",
      "  http://127.0.0.1:8787 and try pairing again (the prompt shows there).",
      "- 'Invalid bridge token' — the code was rotated in ClarityOS →",
      "  Settings → Connections. Pair again.",
      "- 'Chrome does not allow page access on chrome://…' — Chrome blocks",
      "  extensions on its own pages. Switch to a normal site tab.",
      "- To unpair: extension Options → Reset to Defaults.",
      "- Logs: chrome://extensions → ClarityOS → 'service worker'.\n",
    ].join("\n");
    fs.writeFileSync(path.join(destPath, "INSTALL.md"), guide);

    // Open the folder in Finder
    shell.showItemInFolder(destPath);
    return { success: true, path: destPath };
  } catch (err) {
    return { success: false, reason: err.message };
  }
});

ipcMain.handle("extension:reveal", () => {
  if (fs.existsSync(CHROME_EXT_SOURCE)) {
    shell.showItemInFolder(CHROME_EXT_SOURCE);
    return true;
  }
  return false;
});

ipcMain.handle("extension:open-chrome-extensions", () => {
  shell.openExternal("https://support.google.com/chrome_webstore/answer/2664769");
  return true;
});

// The overlay is the only window; if it is gone there is nothing left to drive,
// so do not linger as an invisible process holding the backend open.
app.on("window-all-closed", () => {
  quitApp("all windows closed");
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
  stopPythonBackend();
});
