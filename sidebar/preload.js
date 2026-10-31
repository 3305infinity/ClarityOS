// Preload for the sidebar window only. The overlay keeps its own preload.js
// untouched; this exposes nothing that can run a tool — the backend remains
// the only thing authorised to do that.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sidebarAPI", {
  hide: () => ipcRenderer.invoke("sidebar:hide"),
  getState: () => ipcRenderer.invoke("sidebar:get-state"),
  setCollapsed: (collapsed) => ipcRenderer.invoke("sidebar:set-collapsed", collapsed),
  setAlwaysOnTop: (on) => ipcRenderer.invoke("sidebar:set-always-on-top", on),
  captureScreen: () => ipcRenderer.invoke("sidebar:capture-screen"),
  // Screen-scan visual. The sidebar is a docked strip, so the scan is drawn by
  // the overlay window, which main borrows for the duration.
  setScan: (on, label) => ipcRenderer.invoke("scan:set", { on, label }),
  // Presentation mode. Persisted by main in sidebar-state.json; switching
  // reuses this same window and the same backend connection.
  getUiMode: () => ipcRenderer.invoke("ui:get-mode"),
  onUiActive: (handler) => {
    const wrapped = (_e, active) => handler(Boolean(active));
    ipcRenderer.on("ui:active", wrapped);
    return () => ipcRenderer.removeListener("ui:active", wrapped);
  },
  setUiMode: (mode) => ipcRenderer.invoke("ui:set-mode", mode),
  onChooseUiMode: (handler) => {
    const wrapped = () => handler();
    ipcRenderer.on("ui:choose-mode", wrapped);
    return () => ipcRenderer.removeListener("ui:choose-mode", wrapped);
  },
  onShown: (handler) => {
    const wrapped = () => handler();
    ipcRenderer.on("sidebar:shown", wrapped);
    return () => ipcRenderer.removeListener("sidebar:shown", wrapped);
  },
});
