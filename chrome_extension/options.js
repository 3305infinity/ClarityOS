// ═══════════════════════════════════════════════════════════════
//  ClarityOS — Extension Options Page
//  Configurable bridge URL and auth token (stored in chrome.storage.sync)
// ═══════════════════════════════════════════════════════════════

const DEFAULTS = {
  bridgeUrl: "ws://127.0.0.1:8765",
  bridgeToken: "",
};

const bridgeUrlEl = document.getElementById("bridgeUrl");
const bridgeTokenEl = document.getElementById("bridgeToken");
const saveBtn = document.getElementById("saveBtn");
const resetBtn = document.getElementById("resetBtn");
const statusEl = document.getElementById("status");

function showStatus(message, ok = true) {
  statusEl.textContent = message;
  statusEl.className = `status ${ok ? "ok" : "err"}`;
  setTimeout(() => {
    statusEl.className = "status";
  }, 3000);
}

async function readArea(area) {
  try {
    return await chrome.storage[area].get(["bridgeUrl", "bridgeToken"]);
  } catch (error) {
    return { __error: String(error?.message || error) };
  }
}

/** Sync first, local as the fallback — the same order the worker resolves. */
async function readSettings() {
  const [sync, local] = await Promise.all([readArea("sync"), readArea("local")]);
  return {
    unreadable: Boolean(sync.__error && local.__error),
    error: sync.__error || local.__error || "",
    bridgeUrl: sync.bridgeUrl || local.bridgeUrl || "",
    bridgeToken: sync.bridgeToken || local.bridgeToken || "",
  };
}

// chrome.storage.sync can be disabled by policy or hit its write quota; the
// local copy is what keeps the bridge paired when that happens. A failure to
// write either area is reported rather than swallowed.
async function writeSettings(values) {
  const results = await Promise.allSettled([
    chrome.storage.sync.set(values),
    chrome.storage.local.set(values),
  ]);
  if (results.every((r) => r.status === "rejected")) {
    throw new Error(String(results[0].reason?.message || results[0].reason));
  }
  return results[0].status === "rejected" ? "local only (sync unavailable)" : "";
}

async function loadSettings() {
  const current = await readSettings();
  if (current.unreadable) {
    showStatus(`Could not read saved settings: ${current.error}`, false);
    bridgeUrlEl.value = DEFAULTS.bridgeUrl;
    return;
  }
  bridgeUrlEl.value = current.bridgeUrl || DEFAULTS.bridgeUrl;
  bridgeTokenEl.value = current.bridgeToken || DEFAULTS.bridgeToken;
}

async function saveSettings() {
  const url = bridgeUrlEl.value.trim();
  const token = bridgeTokenEl.value.trim();

  if (!url) {
    showStatus("Bridge URL is required.", false);
    return;
  }

  try {
    new URL(url);
  } catch {
    showStatus("Invalid URL format. Use ws://host:port", false);
    return;
  }

  // An empty field is not an instruction to unpair. It is also what you get if
  // storage was unreadable when this page loaded, so saving over a stored code
  // with "" would destroy a working pairing for no reason.
  if (!token) {
    const current = await readSettings();
    if (current.bridgeToken) {
      await writeSettings({ bridgeUrl: url, bridgeToken: current.bridgeToken }).catch(() => {});
      bridgeTokenEl.value = current.bridgeToken;
      showStatus("Bridge URL saved. Pairing code left unchanged — use Reset to clear it.", false);
      chrome.runtime.sendMessage({ type: "clarityos_settings_updated" });
      return;
    }
  }

  let note = "";
  try {
    note = await writeSettings({ bridgeUrl: url, bridgeToken: token });
  } catch (error) {
    showStatus(`Could not save settings: ${error.message}`, false);
    return;
  }

  // Read it back: "saved" should mean stored, not merely attempted.
  const stored = await readSettings();
  if (stored.bridgeToken !== token) {
    showStatus("Save did not persist — the pairing code is still not stored.", false);
    return;
  }

  showStatus(
    token
      ? `Pairing code saved${note ? ` (${note})` : ""}. Reconnecting…`
      : "Bridge URL saved. No pairing code set yet.",
    Boolean(token),
  );

  // Notify background script to reconnect
  chrome.runtime.sendMessage({ type: "clarityos_settings_updated" });
}

async function resetToDefaults() {
  // Reset clears the pairing code, so make that an explicit choice.
  if (!confirm("Clear the saved pairing code and reset the bridge URL?")) return;
  try {
    await writeSettings(DEFAULTS);
  } catch (error) {
    showStatus(`Could not reset settings: ${error.message}`, false);
    return;
  }
  bridgeUrlEl.value = DEFAULTS.bridgeUrl;
  bridgeTokenEl.value = DEFAULTS.bridgeToken;
  showStatus("Reset to defaults. Paste a new pairing code to reconnect.");
  chrome.runtime.sendMessage({ type: "clarityos_settings_updated" });
}

saveBtn.addEventListener("click", saveSettings);
resetBtn.addEventListener("click", resetToDefaults);

document.addEventListener("DOMContentLoaded", loadSettings);
