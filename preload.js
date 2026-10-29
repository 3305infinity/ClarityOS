const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("overlayAPI", {
  hideWindow: () => ipcRenderer.invoke("overlay:hide"),
  // Presentation mode — persisted in main, never only in renderer memory.
  getUiMode: () => ipcRenderer.invoke("ui:get-mode"),
  onUiActive: (handler) => {
    const wrapped = (_e, active) => handler(Boolean(active));
    ipcRenderer.on("ui:active", wrapped);
    return () => ipcRenderer.removeListener("ui:active", wrapped);
  },
  setUiMode: (mode) => ipcRenderer.invoke("ui:set-mode", mode),
  onChooseUiMode: (handler) => {
    ipcRenderer.on("ui:choose-mode", handler);
    return () => ipcRenderer.removeListener("ui:choose-mode", handler);
  },
  quitApp: () => ipcRenderer.invoke("app:quit"),
  enableMouse: () => ipcRenderer.send("enable-mouse"),
  disableMouse: () => ipcRenderer.send("disable-mouse"),
  onStartListening: (handler) => {
    ipcRenderer.on("start-listening", handler);
    return () => ipcRenderer.removeListener("start-listening", handler);
  },
  onResetOverlayPosition: (handler) => {
    ipcRenderer.on("overlay-reset-position", handler);
    return () => ipcRenderer.removeListener("overlay-reset-position", handler);
  },
  onOverlayHidden: (handler) => {
    ipcRenderer.on("overlay-hidden", handler);
    return () => ipcRenderer.removeListener("overlay-hidden", handler);
  },
  logError: (msg) => ipcRenderer.send("log-error", msg),
  logInfo: (msg) => ipcRenderer.send("log-info", msg),

  // ── Auth / Credentials ──
  loadCredentials: () => ipcRenderer.invoke("auth:load-credentials"),
  saveCredentials: (creds) => ipcRenderer.invoke("auth:save-credentials", creds),
  generateUserId: () => ipcRenderer.invoke("auth:generate-user-id"),
  clearCredentials: () => ipcRenderer.invoke("auth:clear-credentials"),
  isFirstLaunch: () => ipcRenderer.invoke("auth:is-first-launch"),

  // ── App info ──
  getVersion: () => ipcRenderer.invoke("app:get-version"),
  isPackaged: () => ipcRenderer.invoke("app:is-packaged"),

  // ── Chrome Extension ──
  exportExtension: () => ipcRenderer.invoke("extension:export"),
  revealExtension: () => ipcRenderer.invoke("extension:reveal"),
  openChromeExtensions: () => ipcRenderer.invoke("extension:open-chrome-extensions"),

  // ── Backend lifecycle ──
  startBackend: () => ipcRenderer.invoke("backend:start"),

  // ── Setup progress (setup.sh stdout forwarded from main process) ──
  onSetupProgress: (handler) => {
    const wrapped = (_, text) => handler(text);
    ipcRenderer.on("setup:progress", wrapped);
    return () => ipcRenderer.removeListener("setup:progress", wrapped);
  },
});
