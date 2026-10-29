// Targeted tests for the MV3 service worker's message contract.
// Run: node tests/extension/test_worker_messages.js
const assert = require("node:assert");
const { boot } = require("./worker_harness");

const tests = [];
const test = (name, fn) => tests.push({ name, fn });
const settle = (ms = 60) => new Promise((r) => setTimeout(r, ms));

test("worker startup completes without throwing", async () => {
  const env = boot({ socket: "fail" });
  assert.strictEqual(env.startupError, null, `startup threw: ${env.startupError && env.startupError.message}`);
});

test("onMessage listener is registered during startup", async () => {
  const env = boot({ socket: "fail" });
  assert.strictEqual(env.listeners.message.length, 1);
});

test("keepalive alarm is created on every worker start", async () => {
  const env = boot({ socket: "fail" });
  assert.strictEqual(env.alarms.length, 1);
  assert.strictEqual(env.alarms[0].name, "clarityos-bridge-keepalive");
  assert.ok(env.alarms[0].info.periodInMinutes <= 0.5);
});

test("get_status answers immediately while the hub is down", async () => {
  const env = boot({ socket: "fail" });
  await settle();
  const t0 = Date.now();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.ok(Date.now() - t0 < 500, "get_status should be synchronous");
  assert.strictEqual(status.authenticated, false);
  assert.strictEqual(status.socketState, 3, "socket must report CLOSED, not a faked state");
});

test("get_status answers while the socket is still connecting", async () => {
  const env = boot({ socket: "hang" });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.strictEqual(status.socketState, 0);
  assert.strictEqual(status.authenticated, false, "CONNECTING must never read as authenticated");
});

test("an open socket is not reported as authenticated before the handshake", async () => {
  const env = boot({ socket: "open" });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.strictEqual(status.socketState, 1);
  assert.strictEqual(status.authenticated, false);
});

test("authenticated only after the hub accepts the handshake", async () => {
  const env = boot({ socket: "open" });
  await settle();
  env.sockets[0].receive({ type: "browser_bridge_hello_ack", ok: true });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.strictEqual(status.authenticated, true);
  assert.strictEqual(status.socketState, 1);
});

test("a rejected handshake reports the error and stays unauthenticated", async () => {
  const env = boot({ socket: "open" });
  await settle();
  env.sockets[0].receive({ type: "browser_bridge_hello_ack", ok: false, message: "Invalid bridge token" });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.strictEqual(status.authenticated, false);
  assert.match(status.lastBridgeError, /Invalid bridge token/);
});

test("storage failure still leaves get_status answerable", async () => {
  const env = boot({ socket: "fail", storageThrows: true });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.ok(status, "a storage error must not leave the popup hanging");
});

test("no pairing code reports a pairing error rather than connecting", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "" } });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.strictEqual(status.socketState, 3);
  assert.match(status.lastBridgeError, /No pairing code/i);
});

test("request_snapshot reports failure when there is no active tab", async () => {
  const env = boot({ socket: "open", activeTab: null });
  await settle();
  const res = await env.send({ type: "moonwalk_request_snapshot" });
  assert.strictEqual(res.ok, false, "must not claim success with no tab");
  assert.match(String(res.error), /no-active-tab|No active tab/i);
});

test("request_snapshot reports failure on a non-injectable page", async () => {
  const env = boot({ socket: "open", activeTab: { id: 3, url: "chrome://settings" } });
  await settle();
  const res = await env.send({ type: "moonwalk_request_snapshot" });
  assert.strictEqual(res.ok, false);
});

test("a restricted chrome:// tab is explained, not reported as a bridge error", async () => {
  const env = boot({ socket: "open", activeTab: { id: 4, url: "chrome://extensions/" } });
  await settle();
  const res = await env.send({ type: "moonwalk_request_snapshot" });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.restricted, true, "must be flagged as a page limitation");
  assert.strictEqual(res.error, "", "a restricted page is not an error to report");
  assert.match(res.note, /does not allow page access/i);
  assert.strictEqual(res.lastBridgeError, "", "lastBridgeError must stay clean");
});

test("an unpaired worker does not prompt the hub on its own", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "" } });
  await settle();
  assert.strictEqual(env.sockets.length, 0,
    "no socket should be opened to ask for pairing without an explicit user action");
});

test("request_pairing opens a socket and asks for approval", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "" } });
  await settle();
  const res = await env.send({ type: "moonwalk_request_pairing" });
  assert.strictEqual(res.ok, true);
  await settle(120);
  assert.strictEqual(env.sockets.length, 1, "pairing should open exactly one socket");
  const first = JSON.parse(env.sockets[0].sent[0]);
  assert.strictEqual(first.type, "browser_pair_request");
  assert.ok(!("token" in first), "a pairing request must not carry a token");
});

test("an approved pairing is stored in both areas and then authenticates", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "" } });
  await settle();
  await env.send({ type: "moonwalk_request_pairing" });
  await settle(120);
  env.sockets[0].receive({ type: "browser_pair_result", ok: true, token: "granted-code" });
  await settle(120);
  assert.strictEqual(env.storage.bridgeToken, "granted-code", "sync not updated");
  assert.strictEqual(env.localStore.bridgeToken, "granted-code", "local mirror not updated");
  const hello = JSON.parse(env.sockets[0].sent[1]);
  assert.strictEqual(hello.type, "browser_bridge_hello");
  assert.strictEqual(hello.token, "granted-code");
});

test("a denied pairing stores nothing and stays unauthenticated", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "" } });
  await settle();
  await env.send({ type: "moonwalk_request_pairing" });
  await settle(120);
  env.sockets[0].receive({ type: "browser_pair_result", ok: false, token: "", message: "Pairing request was denied in ClarityOS." });
  await settle(120);
  assert.ok(!env.storage.bridgeToken, "nothing may be stored on denial");
  assert.ok(!env.localStore.bridgeToken, "nothing may be mirrored on denial");
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.strictEqual(status.authenticated, false);
  assert.match(status.lastBridgeError, /denied/i);
});

test("a granted pairing code is never logged", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "" } });
  await settle();
  await env.send({ type: "moonwalk_request_pairing" });
  await settle(120);
  env.sockets[0].receive({ type: "browser_pair_result", ok: true, token: "granted-secret-xyz" });
  await settle(120);
  const all = env.logs.concat(env.errors).join("\n");
  assert.ok(!all.includes("granted-secret-xyz"), "granted code leaked into logs");
});

// ── Pairing-code resolution ──

test("pairing code saved in sync is the one the worker uses", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "code-from-sync" } });
  await settle();
  assert.strictEqual(JSON.parse(env.sockets[0].sent[0]).token, "code-from-sync");
});

test("falls back to the local mirror when sync has no code", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "" }, localStorage: { bridgeToken: "code-from-local" } });
  await settle();
  assert.ok(env.sockets.length, "should have attempted a connection");
  assert.strictEqual(JSON.parse(env.sockets[0].sent[0]).token, "code-from-local");
});

test("falls back to the local mirror when sync is unavailable", async () => {
  const env = boot({ socket: "open", syncThrows: true, localStorage: { bridgeToken: "code-from-local" } });
  await settle();
  assert.ok(env.sockets.length, "a sync outage must not block pairing");
  assert.strictEqual(JSON.parse(env.sockets[0].sent[0]).token, "code-from-local");
});

test("a sync-only code is mirrored into local for the next start", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "code-from-sync" } });
  await settle(120);
  assert.strictEqual(env.localStore.bridgeToken, "code-from-sync");
});

test("unreadable storage is reported as such, not as a missing pairing code", async () => {
  const env = boot({ socket: "open", storageThrows: true });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.match(status.lastBridgeError, /Could not read extension settings/i);
  assert.doesNotMatch(status.lastBridgeError, /No pairing code/i);
  assert.strictEqual(status.authenticated, false);
});

test("the pairing code is never written to the worker log", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "super-secret-code" } });
  await settle();
  env.sockets[0].receive({ type: "browser_bridge_hello_ack", ok: true });
  await settle();
  const all = env.logs.concat(env.errors).join("\n");
  assert.ok(!all.includes("super-secret-code"), "pairing code leaked into logs");
});

test("status never exposes the pairing code", async () => {
  const env = boot({ socket: "open", storage: { bridgeToken: "super-secret-code" } });
  await settle();
  const status = await env.send({ type: "moonwalk_get_status" });
  assert.ok(!JSON.stringify(status).includes("super-secret-code"), "pairing code leaked into status");
});

test("unknown message types do not leave the popup waiting", async () => {
  const env = boot({ socket: "fail" });
  await settle();
  await assert.rejects(
    () => env.send({ type: "not_a_real_message" }, 1000),
    /PORT_CLOSED/,
    "an unhandled type must close the port, not hang",
  );
});

test("every popup message type has a handler that responds", async () => {
  const env = boot({ socket: "open" });
  await settle();
  for (const type of ["moonwalk_get_status", "moonwalk_request_snapshot", "moonwalk_ping_bridge"]) {
    const res = await env.send({ type }, 2000);
    assert.ok(res, `${type} returned no response`);
  }
});

(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      console.log(`  PASS  ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`  FAIL  ${name}\n        ${error.message}`);
    }
  }
  console.log(`\n${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
