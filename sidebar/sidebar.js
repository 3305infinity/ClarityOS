/* ClarityOS sidebar renderer.
 *
 * Deliberately NOT a second agent. This speaks the same WebSocket protocol to
 * the same voice server (:8000) that renderer/renderer.js uses, so every task
 * runs through the existing agent, tool registry, policy engine and approval
 * broker. Nothing here invokes a tool directly.
 */

const WS_URL = "ws://127.0.0.1:8000/ws";
const api = window.sidebarAPI || {};

const $ = (id) => document.getElementById(id);
const logEl = $("log");
const inputEl = $("in");
const chipEl = $("chip");
const chipText = $("chip-text");

const S = {
  ws: null,
  retry: 700,
  busy: false,
  lastAi: null,          // the assistant bubble currently being appended to
  approvalId: null,
  audio: null,
  queue: [],
  playing: false,
  greeted: false,
  mic: { active: false, stream: null, ctx: null, source: null, processor: null },
};

/* ── Chat rendering ── */

function clearEmpty() {
  const empty = logEl.querySelector(".empty");
  if (empty) empty.remove();
}

function bubble(cls, text) {
  clearEmpty();
  const node = document.createElement("div");
  node.className = `m ${cls}`;
  node.textContent = text;
  logEl.append(node);
  logEl.scrollTop = logEl.scrollHeight;
  return node;
}

function setState(state, label) {
  chipEl.className = `chip state-${state}`;
  chipText.textContent = label;
  const working = state === "thinking" || state === "doing";
  S.busy = working;
  $("b-stop").hidden = !working;
  $("b-send").hidden = working;
}

function toast(text, ms = 3200) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

/* ── Transport ── */

function send(payload) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    S.ws.send(JSON.stringify(payload));
    return true;
  }
  toast("ClarityOS backend is not connected yet.");
  return false;
}

function connect() {
  // Never stack sockets. The "close" handler and the constructor's catch both
  // schedule a retry, so a backend that is slow to come up produced several
  // overlapping connections — and the voice server builds one agent per
  // connection, so the user ended up with three agents for one window.
  if (S.ws && (S.ws.readyState === WebSocket.OPEN ||
               S.ws.readyState === WebSocket.CONNECTING)) return;
  if (S.retryTimer) { clearTimeout(S.retryTimer); S.retryTimer = null; }
  let ws;
  try {
    ws = new WebSocket(WS_URL);
  } catch (_) {
    S.retryTimer = setTimeout(connect, S.retry);
    return;
  }
  S.ws = ws;

  ws.addEventListener("open", () => {
    S.retry = 700;
    setState("idle", "Idle");
    // Greet once per session, and only on the first successful connect — not
    // after every reconnect, and never on top of an existing conversation.
    if (!S.greeted && !logEl.querySelector(".m")) {
      S.greeted = true;
      bubble("ai", "Hey! How can I help you today?");
    }
  });

  ws.addEventListener("message", (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }
    handle(msg);
  });

  ws.addEventListener("close", () => {
    setState("error", "Offline");
    if (S.ws === ws) S.ws = null;        // only the live socket may retry
    if (!S.retryTimer) S.retryTimer = setTimeout(connect, S.retry);
    S.retry = Math.min(S.retry * 2, 8000);
  });

  ws.addEventListener("error", () => { try { ws.close(); } catch (_) {} });
}

/* ── Server events (same names renderer/renderer.js handles) ── */

function handle(msg) {
  if (msg.type === "thinking" || msg.type === "progress" || msg.state === "state-loading") {
    setState("thinking", "Thinking");
    return;
  }

  if (msg.type === "doing") {
    setState("doing", msg.app || msg.text || "Working");
    if (msg.text) {
      const line = bubble("sys", msg.text);
      line.dataset.transient = "1";
    }
    return;
  }

  if (msg.type === "response" || msg.type === "action") {
    // The backend sends {type:"response", payload:{text|message, …}} — the same
    // shape renderer.js reads. This used to look at msg.text / msg.message,
    // which are always undefined here, so every agent answer rendered as an
    // empty bubble: spoken by TTS, never shown. That is what made a completed
    // task look like it was still hanging.
    const payload = msg.payload || {};
    const structured = payload.modal_data || (payload.modal ? payload : null);
    const text = String(
      payload.text || payload.message ||
      (structured ? structured.text || structured.message || "" : "") || "",
    );

    if (text) {
      if (!S.lastAi) S.lastAi = bubble("ai", "");
      S.lastAi.textContent += text;
      logEl.scrollTop = logEl.scrollHeight;
    }

    // A plan gate arrives as a normal response carrying modal_data with the
    // planned steps and a plan_id. Only the sentence was being rendered, so
    // the user saw "Review this plan and choose Proceed" with no plan and
    // nothing to click. Show the steps and wire real buttons.
    if (structured && (structured.modal === "plan" || Array.isArray(structured.steps))) {
      showPlan(structured);
      S.lastAi = null;
      setState("idle", "Idle");
      return;
    }

    // `streaming` is only present on partial chunks; its absence means this is
    // the terminal event for the task.
    if (!payload.streaming && !msg.streaming) {
      if (!text && !S.lastAi) {
        // Terminal event with nothing to show: say so rather than leaving a
        // silent gap the user reads as a hang.
        bubble("err", "The assistant finished without returning any text.");
      }
      S.lastAi = null;
      setState("idle", "Idle");
    }
    return;
  }

  if (msg.type === "ack") { setState("thinking", "Thinking"); return; }

  // Dictation results come back on the existing transcript events.
  if (msg.type === "transcribing") { setState("thinking", "Transcribing"); return; }
  if (msg.type === "transcript") {
    stopDictation({ finalize: false });
    if (msg.text) { bubble("me", msg.text); S.lastAi = null; }
    setState("thinking", "Thinking");
    return;
  }
  if (msg.type === "transcript_error") {
    stopDictation({ finalize: false });
    bubble("err", `${msg.message || "Could not transcribe that."}${msg.hint ? " " + msg.hint : ""}`);
    setState("idle", "Idle");
    return;
  }

  if (msg.type === "approval_request") { showApproval(msg); return; }
  if (msg.type === "approval_resolved") { hideApproval(); return; }

  if (msg.type === "tool_blocked") {
    bubble("err", `Blocked: ${msg.tool || "tool"} — ${msg.reason || "not permitted"}`);
    setState("idle", "Idle");
    return;
  }

  if (msg.type === "tts_chunk") { enqueueAudio(msg); return; }
  if (msg.type === "tts_stop") { stopAudio(); return; }

  if (msg.type === "error") {
    bubble("err", msg.message || "Something went wrong.");
    setState("idle", "Idle");
    return;
  }

  const stateStr = msg.state || null;
  if (stateStr === "state-idle") setState("idle", "Idle");
  else if (stateStr === "state-listening") setState("listening", "Listening");
}

/* ── Approvals — answered with the existing approval_response message ── */

function showApproval(msg) {
  S.approvalId = msg.approval_id || msg.id || "";
  $("ap-why").textContent = msg.reason || msg.why || "ClarityOS wants to run a sensitive action.";
  $("ap-sum").textContent = msg.summary || msg.detail || msg.tool || "";
  $("approve").hidden = false;
}

function hideApproval() {
  $("approve").hidden = true;
  S.approvalId = null;
}

function showPlan(data) {
  const steps = Array.isArray(data.steps) ? data.steps : [];
  S.planId = data.plan_id || "";

  const card = document.createElement("div");
  card.className = "m plan-card";

  const title = document.createElement("p");
  title.className = "ap-t";
  title.textContent = "Plan — review before it runs";
  card.appendChild(title);

  if (steps.length) {
    const list = document.createElement("ol");
    list.className = "plan-steps";
    steps.forEach((st) => {
      const li = document.createElement("li");
      const label = document.createElement("strong");
      label.textContent = String(st.label || st.title || "Step");
      li.appendChild(label);
      const detail = String(st.detail || st.description || "");
      if (detail) {
        const d = document.createElement("span");
        d.className = "plan-detail";
        d.textContent = " — " + detail;
        li.appendChild(d);
      }
      list.appendChild(li);
    });
    card.appendChild(list);
  } else {
    const empty = document.createElement("p");
    empty.className = "ap-why";
    empty.textContent = "The planner did not return any steps for this request.";
    card.appendChild(empty);
  }

  const btns = document.createElement("div");
  btns.className = "ap-btns";
  const no = document.createElement("button");
  no.className = "deny";
  no.textContent = "Cancel";
  const yes = document.createElement("button");
  yes.className = "allow";
  yes.textContent = "Proceed";

  // Replies go back as ordinary text_input, which is what the backend's
  // await_reply is already waiting on — no new protocol, and the same path a
  // typed "proceed" takes. Buttons disable on click so one plan runs once.
  const answer = (approve) => {
    if (card.dataset.answered) return;
    card.dataset.answered = "1";
    yes.disabled = no.disabled = true;
    bubble("me", approve ? "Proceed" : "Cancel");
    send({ type: "text_input", text: approve ? "proceed" : "cancel" });
    setState("thinking", approve ? "Thinking" : "Cancelling");
  };
  yes.addEventListener("click", () => answer(true));
  no.addEventListener("click", () => answer(false));
  btns.appendChild(no);
  btns.appendChild(yes);
  card.appendChild(btns);

  logEl.appendChild(card);
  logEl.scrollTop = logEl.scrollHeight;
}

function answerApproval(approve) {
  if (!S.approvalId) return hideApproval();
  send({ type: "approval_response", approval_id: S.approvalId, approve });
  bubble("sys", approve ? "Approved" : "Denied");
  hideApproval();
}

/* ── Spoken replies: same tts_chunk / tts_done contract as the overlay ── */

function audioCtx() {
  if (!S.audio) S.audio = new (window.AudioContext || window.webkitAudioContext)();
  return S.audio;
}

async function enqueueAudio(msg) {
  const b64 = msg.audio || msg.data || "";
  if (!b64) return;
  try {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const buffer = await audioCtx().decodeAudioData(bytes.buffer);
    S.queue.push(buffer);
    if (!S.playing) playNext();
  } catch (_) {
    // Undecodable chunk: drop it rather than breaking the queue.
  }
}

function playNext() {
  const buffer = S.queue.shift();
  if (!buffer) {
    S.playing = false;
    setState("idle", "Idle");
    // Tell the backend playback finished so it can release its mic gate.
    send({ type: "tts_done" });
    return;
  }
  S.playing = true;
  setState("speaking", "Speaking");
  const source = audioCtx().createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx().destination);
  source.onended = playNext;
  source.start();
  S.source = source;
}

function stopAudio() {
  S.queue.length = 0;
  try { S.source && S.source.stop(); } catch (_) {}
  S.playing = false;
  setState("idle", "Idle");
}

/* ── Sending ── */

function submit(text) {
  const value = (text ?? inputEl.value).trim();
  if (!value) return;
  bubble("me", value);
  S.lastAi = null;
  if (!send({ type: "text_input", text: value })) return;
  inputEl.value = "";
  inputEl.style.height = "auto";
  setState("thinking", "Thinking");
}

function cancel() {
  if (S.mic.active) stopDictation({ finalize: false });
  send({ type: "cancel_task" });
  stopAudio();
  bubble("sys", "Cancelled");
  setState("idle", "Idle");
}

/* ── Screen capture: explicit, one shot, never continuous ── */

async function askAboutScreen() {
  const question = inputEl.value.trim() ||
    "Summarise what is currently on my screen and tell me what I can do next.";
  toast("Capturing your screen…");
  let shot = null;
  try {
    shot = await api.captureScreen?.();
  } catch (error) {
    bubble("err", `Screen capture failed: ${error.message || error}`);
    return;
  }
  if (!shot || !shot.ok) {
    bubble("err", shot?.error || "Screen capture is unavailable on this machine.");
    return;
  }

  // Show the user exactly what was captured before anything is sent onward.
  const me = bubble("me", question);
  const img = document.createElement("img");
  img.className = "shot";
  img.src = shot.dataUrl;
  img.alt = "Captured screen";
  me.append(img);

  S.lastAi = null;
  // The agent reads the screen through its own read_screen / get_ui_tree tools,
  // which run under the policy engine. The preview above is for the user.
  if (send({ type: "text_input", text: question })) {
    setState("thinking", "Thinking");
    inputEl.value = "";
  }
}

/* ── Voice input ──
   Push-to-talk over the SAME protocol the overlay uses: 16 kHz mono PCM in a
   WAV wrapper, base64, sent as `audio_chunk`. The backend does the silence
   detection and replies with `transcript`, then runs the task through the
   normal pipeline — no second STT path, no second agent. */

function writeString(view, offset, text) {
  for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
}

function encodeWAVChunk(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  writeString(view, 0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(view, 8, "WAVE");
  writeString(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(view, 36, "data");
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i += 1, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

async function startDictation() {
  if (S.mic.active) return;
  if (!S.ws || S.ws.readyState !== WebSocket.OPEN) {
    bubble("err", "Not connected to the ClarityOS backend yet — try again in a moment.");
    return;
  }
  try {
    S.mic.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
  } catch (error) {
    bubble("err", `Microphone unavailable: ${error.message || error}. `
      + `Allow microphone access for ClarityOS in Windows Settings → Privacy → Microphone.`);
    return;
  }
  // Tell the backend to listen. Without this it stays in IDLE and feeds every
  // chunk to the wake-word detector, which is disabled when pvporcupine is not
  // installed — so audio streamed fine and was silently dropped. The overlay
  // already sends this on its hotkey; the Speak button never did.
  send({ type: "hotkey_pressed" });

  S.mic.ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
  S.mic.source = S.mic.ctx.createMediaStreamSource(S.mic.stream);
  S.mic.processor = S.mic.ctx.createScriptProcessor(1024, 1, 1);
  S.mic.processor.onaudioprocess = (event) => {
    if (!S.ws || S.ws.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    send({ type: "audio_chunk", payload: arrayBufferToBase64(encodeWAVChunk(input, S.mic.ctx.sampleRate)) });
  };
  S.mic.source.connect(S.mic.processor);
  S.mic.processor.connect(S.mic.ctx.destination);

  S.mic.active = true;
  $("b-mic").classList.add("recording");
  setState("listening", "Listening");
  toast("Listening — click Speak again to stop.");
}

function stopDictation({ finalize = true } = {}) {
  const m = S.mic;
  // The backend ends a command on sustained silence (7 chunks ≈ 0.45s), so
  // simply cutting the stream would leave whatever was said sitting in its
  // buffer and the server stuck in LISTENING. Flush a short tail of real
  // silence so the existing detector finalises the turn — no new protocol.
  //
  // finalize:false is for the cases where the utterance should be abandoned
  // rather than submitted — cancelling, or this window going away mid-sentence.
  if (finalize && m.active && m.ctx && S.ws && S.ws.readyState === WebSocket.OPEN) {
    const silence = new Float32Array(1024);
    for (let i = 0; i < 10; i += 1) {
      send({ type: "audio_chunk",
             payload: arrayBufferToBase64(encodeWAVChunk(silence, m.ctx.sampleRate)) });
    }
  }
  try { m.processor && m.processor.disconnect(); } catch (_) {}
  try { m.source && m.source.disconnect(); } catch (_) {}
  try { m.ctx && m.ctx.close(); } catch (_) {}
  try { m.stream && m.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
  m.processor = m.source = m.ctx = m.stream = null;
  m.active = false;
  $("b-mic").classList.remove("recording");
  if (!S.busy) setState("idle", "Idle");
}

/* ── Wiring ── */

$("b-send").addEventListener("click", () => submit());
$("b-stop").addEventListener("click", cancel);
$("b-screen").addEventListener("click", askAboutScreen);
$("b-mic").addEventListener("click", () => (S.mic.active ? stopDictation() : startDictation()));
$("ap-yes").addEventListener("click", () => answerApproval(true));
$("ap-no").addEventListener("click", () => answerApproval(false));

inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = `${Math.min(inputEl.scrollHeight, 120)}px`;
});

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!$("approve").hidden) return answerApproval(false);
    if (S.busy || S.playing) return cancel();
    api.hide?.();
  }
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t === tab));
    document.querySelectorAll(".panel").forEach((p) => {
      p.classList.toggle("on", p.id === `p-${tab.dataset.tab}`);
    });
  });
});

document.querySelectorAll(".act").forEach((btn) => {
  btn.addEventListener("click", () => submit(btn.dataset.cmd));
});

// Apps are plain launch requests handled by the agent's open_app tool, which is
// proxied to the ClarityOS Windows helper.
const APPS = ["Spotify", "Chrome", "VS Code", "Notepad", "Explorer", "Calculator", "Settings", "Terminal"];
const appGrid = $("appgrid");
for (const app of APPS) {
  const btn = document.createElement("button");
  btn.className = "appbtn";
  btn.textContent = app;
  btn.addEventListener("click", () => submit(`Open ${app}`));
  appGrid.append(btn);
}

/* ── Theme ──
   Following the OS alone meant a light sidebar was unreachable on a dark
   Windows install. An explicit choice is stored and wins over the system; the
   CSS already honours data-theme. */
const THEME_KEY = "clarityos.sidebar.theme";

function systemTheme() {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = $("b-theme");
  if (btn) btn.title = theme === "light" ? "Switch to dark" : "Switch to light";
}

(function restoreTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem(THEME_KEY);
  } catch (_) {
    saved = null;
  }
  applyTheme(saved === "light" || saved === "dark" ? saved : systemTheme());
})();

$("b-theme").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  applyTheme(next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (_) {
    // Not persisted — the choice still applies for this session.
  }
});

$("b-hide").addEventListener("click", () => api.hide?.());
$("b-collapse").addEventListener("click", async () => {
  const collapsed = document.body.classList.toggle("collapsed");
  await api.setCollapsed?.(collapsed);
});
$("b-pin").addEventListener("click", async () => {
  const next = $("b-pin").getAttribute("aria-pressed") !== "true";
  $("b-pin").setAttribute("aria-pressed", String(next));
  await api.setAlwaysOnTop?.(next);
  toast(next ? "Always on top" : "Normal window order");
});

api.onShown?.(() => inputEl.focus());

// Always start expanded: a restored collapsed state repeatedly produced a
// 52px window still laid out at full width, with every control off-screen.
document.body.classList.remove("collapsed");

connect();
