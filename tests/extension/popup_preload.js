// chrome.runtime stub for the popup retry tests. Mimics how Chrome behaves for
// a worker that is cold (first message hangs while it boots), dead (every
// message is refused), or healthy.
const arg = process.argv.find((a) => a.startsWith("--scenario=")) || "--scenario=healthy";
const scenario = arg.split("=")[1];

const STATUS = {
  authenticated: true,
  sessionId: "chrome-session-1730000000000",
  bridgeUrl: "ws://127.0.0.1:8765",
  extensionName: "ClarityOS companion",
  socketState: 1,
  lastBridgeError: "",
  lastBridgeMessage: "Handshake accepted",
  snapshotCount: 1,
  latestSnapshot: {
    session_id: "chrome-session-1730000000000",
    tab_id: 42,
    url: "https://example.com/",
    title: "Example Domain",
    generation: Date.now() - 4000,
    elements: new Array(87).fill({}),
  },
};

window.__attempts = 0;

window.chrome = {
  runtime: {
    sendMessage(payload) {
      window.__attempts += 1;
      if (scenario === "dead") {
        return Promise.reject(new Error("Could not establish connection. Receiving end does not exist."));
      }
      if (scenario === "slow-wake" && window.__attempts === 1) {
        return new Promise(() => {}); // worker still booting: never settles
      }
      const body = payload?.type === "moonwalk_request_snapshot" ? { ok: true, ...STATUS } : STATUS;
      return new Promise((resolve) => setTimeout(() => resolve(body), 40));
    },
  },
};
