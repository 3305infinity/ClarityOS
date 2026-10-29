// Loads the real chrome_extension/background.js under a mock MV3 environment so
// worker startup and the popup's message round-trip can be tested without Chrome.
//
//   const h = require("./worker_harness");
//   const env = h.boot({ socket: "fail" });
//   await env.send({ type: "moonwalk_get_status" });
//
// `socket` controls the fake WebSocket: "open" (connects and accepts the
// handshake), "fail" (errors immediately, i.e. hub down), or "hang".

const path = require("path");
const Module = require("module");

const BACKGROUND = path.join(__dirname, "..", "..", "chrome_extension", "background.js");

function makeWebSocket(mode, onCreate) {
  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this._listeners = {};
      onCreate(this);
      if (mode === "open") {
        setTimeout(() => {
          this.readyState = 1;
          this._emit("open", {});
        }, 5);
      } else if (mode === "fail") {
        setTimeout(() => {
          this.readyState = 3;
          this._emit("error", { message: "connection refused" });
          this._emit("close", {});
        }, 5);
      }
      // "hang" stays CONNECTING forever
    }
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    }
    _emit(type, event) {
      for (const fn of this._listeners[type] || []) fn(event);
    }
    send(data) {
      this.sent.push(data);
    }
    close() {
      this.readyState = 3;
      this._emit("close", {});
    }
    /** Simulate the hub replying. */
    receive(obj) {
      this._emit("message", { data: JSON.stringify(obj) });
    }
  }
  return FakeWebSocket;
}

function boot(opts = {}) {
  const storage = Object.assign({ bridgeUrl: "ws://127.0.0.1:8765", bridgeToken: "test-token" }, opts.storage);
  const sockets = [];
  const alarms = [];
  const listeners = { message: [], alarm: [], installed: [], startup: [], activated: [], updated: [] };
  const logs = [];
  const errors = [];

  // Two independent areas, each able to fail on its own — chrome.storage.sync
  // can be disabled by policy or out of quota while local still works.
  const localStore = Object.assign({}, opts.localStorage);
  const stores = { sync: storage, local: localStore };
  const throws = {
    sync: Boolean(opts.storageThrows || opts.syncThrows),
    local: Boolean(opts.storageThrows || opts.localThrows),
  };
  const area = (name) => ({
    get: async (keys) => {
      if (throws[name]) throw new Error(`${name} storage unavailable`);
      const out = {};
      for (const k of [].concat(keys)) if (k in stores[name]) out[k] = stores[name][k];
      return out;
    },
    set: async (values) => {
      if (throws[name]) throw new Error(`${name} storage unavailable`);
      Object.assign(stores[name], values);
    },
  });

  const chrome = {
    storage: { sync: area("sync"), local: area("local") },
    alarms: {
      create: (name, info) => alarms.push({ name, info }),
      onAlarm: { addListener: (fn) => listeners.alarm.push(fn) },
    },
    runtime: {
      onMessage: { addListener: (fn) => listeners.message.push(fn) },
      onInstalled: { addListener: (fn) => listeners.installed.push(fn) },
      onStartup: { addListener: (fn) => listeners.startup.push(fn) },
      lastError: null,
    },
    tabs: {
      onActivated: { addListener: (fn) => listeners.activated.push(fn) },
      onUpdated: { addListener: (fn) => listeners.updated.push(fn) },
      query: async () => (opts.activeTab === null ? [] : [opts.activeTab || { id: 7, url: "https://example.com" }]),
      get: async (tabId) => {
        if (opts.activeTab === null) throw new Error("No tab with id");
        const tab = opts.activeTab || { id: 7, url: "https://example.com" };
        return { ...tab, id: tab.id ?? tabId };
      },
      sendMessage: async () => {
        if (opts.contentScriptMissing) throw new Error("Receiving end does not exist.");
        return { ok: true };
      },
    },
    scripting: { executeScript: async () => [{ result: true }] },
  };

  const FakeWebSocket = makeWebSocket(opts.socket || "fail", (s) => sockets.push(s));

  const sandboxGlobals = {
    chrome,
    WebSocket: FakeWebSocket,
    console: {
      log: (...a) => logs.push(a.join(" ")),
      warn: (...a) => logs.push(a.join(" ")),
      error: (...a) => errors.push(a.join(" ")),
    },
  };

  // Evaluate background.js with the mocks injected as parameters, so a throw
  // during "worker startup" surfaces here exactly as Chrome would see it.
  const fs = require("fs");
  const source = fs.readFileSync(BACKGROUND, "utf8");
  const names = Object.keys(sandboxGlobals);
  let startupError = null;
  try {
    // eslint-disable-next-line no-new-func
    const factory = new Function(...names, `"use strict";\n${source}\n;return { get bridgeStatusFn(){ return typeof bridgeStatus === "function" ? bridgeStatus : null; } };`);
    factory(...names.map((n) => sandboxGlobals[n]));
  } catch (error) {
    startupError = error;
  }

  // Mirrors chrome.runtime.sendMessage: resolves only if a listener calls
  // sendResponse; rejects if every listener declines (port closed).
  function send(message, timeoutMs = 4000) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (!settled) { settled = true; reject(new Error("TIMEOUT: no response within " + timeoutMs + "ms")); }
      }, timeoutMs);
      let keepOpen = false;
      const sendResponse = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      for (const fn of listeners.message) {
        if (fn(message, { tab: null }, sendResponse) === true) keepOpen = true;
      }
      if (!settled && !keepOpen) {
        settled = true;
        clearTimeout(timer);
        reject(new Error("PORT_CLOSED: no listener handled the message"));
      }
    });
  }

  return { chrome, listeners, alarms, sockets, logs, errors, startupError, send, storage, localStore };
}

module.exports = { boot };
