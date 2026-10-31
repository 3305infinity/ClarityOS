# ClarityOS

**An AI assistant that controls your Windows PC — and refuses to lie to you about it.**

Voice or text in. Real apps, real tabs, real files out. Built with Electron + Python, thinking with Gemini, acting through a local Hub, a Chrome extension and a native Windows helper.

---

## The problem we actually set out to solve

Desktop agents are easy to demo and hard to trust. Ours was no different at first. Real failures from our own logs:

- Asked for the weather in Patna, it opened four URLs, read a Google *support* page, and reported **30°C / 45%** confidently. The truth was 25.4°C / 86%.
- Asked to summarise the open browser tab, it summarised **its own dashboard**, which happened to be the active Chrome tab.
- Asked to read a file, it returned the editor's menu bar — `File Edit Selection View Go Run` — and called that the source code.
- A click timed out; the tool reported `ok: true` anyway, so every later step believed the page had navigated.

Every one of these produced a *confident, well-formatted, completely wrong answer*. A demo would have sailed past all four.

**So ClarityOS is built around one rule: an answer must be traceable to something the agent actually retrieved.** Most of the engineering below exists to enforce that.

---

## Guardrails

These are the parts we are proud of. Each one exists because it caught a real bug.

### Retrieval validation boundary
[`backend/agent/retrieval_guard.py`](backend/agent/retrieval_guard.py)

Every tool result is classified before it can become part of an answer. The guard is **source-aware** — it knows what an editor read, a screenshot, a browser page and an accessibility tree each look like, and what the request actually asked for.

| It rejects | Because |
|---|---|
| A UI tree holding only tab strips and toolbars | Window furniture is not page content |
| A read of ClarityOS's own `127.0.0.1:8787` dashboard | That is us, not the page you asked about |
| Page text whose domain ≠ the live tab's domain | The extension had not re-read after navigating |
| A screenshot answering a *browser page* question, while the extension is connected | A paraphrase of pixels is not the page text — and it was avoidable |
| An editor read with no code-like evidence in it | Three code signals, not a length check |

A rejected result cannot become an answer, and the refusal says what to do instead.

### Answers can't outrun the evidence
If every retrieval failed, the final reply cannot be *"Here is the current weather in Patna."* — a sentence that promises content followed by none. The guard detects content-promising stubs and replaces them with the real reason.

**URLs are grounded the same way.** A link may only appear in an answer if it came from content retrieved and validated *in that task*. Otherwise it is the model's recollection — which is how a confident, wrong hackathon URL once got handed over.

### Loops cost money, so they're capped
| Budget | Limit | Why |
|---|---|---|
| Navigations per task | 3 | One run opened four URLs for a single question |
| Vision calls per milestone | 2 | Each is 20–55s and a Gemini call; one milestone made ten |
| Repeated identical results | 3 → stop | Fingerprints the **result**, not the arguments — the model rewords its arguments while the tool returns byte-identical output |
| Navigation after content is in hand | 1 more source | The first page often already answered it |

### Tools report what happened, not what was attempted
A click that is queued but never confirmed returns `ok: false`, not `ok: true`. A Spotify search that opens but does not play says exactly that. Navigation and reading are pinned to **the same browser**, so the agent can never navigate in one and read from another.

### Honest failure
When a step fails, the reply names the reason — not just the goal it failed to reach. *"I couldn't finish "Find 3 similar movies" — get_ui_tree produced the same result 3 times"* is actionable; *"Sorry, I couldn't complete: Find 3 similar movies"* is not.

---

## Security model

Capability-based, deny-by-default, and shared by **both** front-ends — one policy implementation, one approval implementation.

- **Policy engine** ([`backend/hub/policy.py`](backend/hub/policy.py)) classifies every tool call. Shell, file writes, messaging, purchases and submits need explicit approval.
- **Approval broker** ([`backend/hub/approval.py`](backend/hub/approval.py)) mints single-use, expiring nonces. Prompts are broadcast to *every* open window, so the one you are looking at can answer. If no UI is reachable, the action is **denied** — an action nobody could see is an action nobody approved.
- **Approving a plan grants consent for that plan only** — scoped to the tools it names, single-use, cleared when a new plan appears, and it can never override a capability you switched off in Settings.
- **Extension pairing** is deny-by-default, expires in two minutes, and names who is asking. The code is provisioned over loopback; you never copy a secret by hand.
- **Secrets never reach a prompt, a log or the UI.** Approval summaries are redacted against live key values. Sensitive files are excluded from editor reads.
- CAPTCHAs and auth walls are **never** bypassed — the agent stops and tells you.

---

## Quick start

```powershell
# 1. Node dependencies
npm install

# 2. Python environment (creates backend\.venv, installs requirements)
bash setup.sh                 # Git Bash

# 3. Configure
copy backend\.env.example backend\.env
# Add GEMINI_API_KEY — or skip this and add it in-app at Settings → API keys

# 4. Launch
npm start
```

On launch ClarityOS asks **how you want to work today** — Floating assistant or Docked sidebar. It asks every time; there is no hidden saved preference to fight.

Local voice (Windows SAPI) needs no API key. Gemini does.

> **Chrome extension:** `chrome://extensions` → Developer mode → **Load unpacked** → select `chrome_extension\`, then **Pair this extension** in the popup and **Approve** in ClarityOS. One approval, once.

---

## What it does

- **Two front-ends, one assistant** — a docked sidebar for long conversations, a draggable glass pill for quick asks. Same backend, same WebSocket protocol, same conversation. Switch from the tray without restarting.
- **Voice-first** — hold `Ctrl+Shift+Space`, speak, watch it work. It narrates milestones as it goes rather than going silent for a minute.
- **Real browser control** — a Chrome extension bridges live tabs: read pages, find elements, click, type, extract structured data. A scan sweeps the screen while it reads, so you can see what it is looking at.
- **Desktop control** — open and drive Windows apps through UI Automation and a native helper.
- **Google Workspace** — read mail, draft replies into a real Gmail compose window, create docs and calendar events.
- **Local Hub** — a dashboard at `127.0.0.1:8787` for conversation, live activity, settings, permissions and connections.
- **Platform-aware planning** — plans target Windows shells and apps, not a borrowed macOS environment.
- **LangGraph (opt-in)** — `CLARITYOS_USE_LANGGRAPH=1` swaps the linear loop for the graph agent.

---

## Architecture

```
Sidebar / overlay ──WS :8000──►  Voice server  ──►  Agent  ──►  Tool registry
                                                      │              │
Hub dashboard     ──WS :8787──►  Hub  ◄── policy + approvals ───────┘
                                    │
Chrome extension  ──WS :8765──►  Browser bridge  (tabs, DOM, forms)
Windows helper    ──WS :8787/helper──►  native actions
```

The sidebar and the overlay are two views of one assistant, not two assistants: same protocol, same voice server, one agent per connection — which is what keeps a single request from being handled twice.

**Agent loop — SPAV:** Sense → Plan → Act → Verify, with milestone-based execution. The planner decomposes a request into milestones with success signals; the executor runs each one, and the retrieval guard sits between every tool result and the answer.

| Port | What |
|------|------|
| `8787` | Hub dashboard + API (HTTP/WS) |
| `8765` | Chrome extension bridge (WS) |
| `8000` | Electron voice server (WS) |

---

## Gemini API key

The key can come from two places, and **a key saved in the app wins**:

1. **Settings → API keys** — encrypted in `credentials.enc` via Electron `safeStorage`
2. **`backend/.env`** — `GEMINI_API_KEY`

That precedence is the usual cause of *"the key in `.env` is correct but ClarityOS says it was rejected"*: a stale key in Settings is shadowing it. Use the tray item **Clear saved Gemini key** to fall back to `.env`; it removes only the key.

Provider failures are classified, and the ones that cannot succeed are not retried:

| Failure | Behaviour |
|---|---|
| `401` / `403` / invalid key | Reported as an auth problem; no retry |
| `429 RESOURCE_EXHAUSTED` | Quota gone; no retry against the exhausted model |
| `503 UNAVAILABLE`, network | Transient; at most one retry, then it stops and says so |

---

## Project structure

```
ClarityOS/
├── main.js                  # Electron main: windows, presentation mode, tray, backend lifecycle
├── renderer/                # Floating assistant — glass-pill overlay
├── sidebar/                 # Docked sidebar — same backend, same protocol
├── web_app/                 # Hub dashboard served at 127.0.0.1:8787
│
├── backend/
│   ├── agent/
│   │   ├── core_v2.py            # SPAV loop, plan gate, URL grounding
│   │   ├── retrieval_guard.py    # the validation boundary
│   │   ├── milestone_executor.py # execution, stall + repeat detection, budgets
│   │   ├── planner.py            # LLM task decomposition
│   │   ├── current_context.py    # binds a request to one window/tab
│   │   └── graph_agent.py        # LangGraph agent (opt-in)
│   ├── hub/
│   │   ├── policy.py             # capability classification
│   │   ├── approval.py           # single-use approval nonces
│   │   └── core.py               # hub state, tasks, guard install
│   ├── browser/                  # extension bridge, pairing, page interpreters
│   ├── servers/                  # voice server, bridge server, voice policy guard
│   ├── tools/                    # browser_*, file, form, media, workspace, vault
│   └── voice/                    # STT/TTS, progress narration
│
├── chrome_extension/        # MV3 worker, content script, popup, options
├── windows_helper/          # native Windows action helper
├── docs/                    # GUIDE.md, ARCHITECTURE.md, LANGGRAPH.md
└── tests/                   # Python + JS suites
```

---

## Testing

```powershell
# Python: hub, policy, browser bridge, pairing
backend\.venv\Scripts\python.exe -m pytest tests\ -q --asyncio-mode=auto

# Chrome extension: worker message contract (real background.js, mocked chrome APIs)
node tests\extension\test_worker_messages.js

# Chrome extension: real Chrome, real Options UI, real worker restart
node tests\extension\test_pairing_roundtrip.js

# End-to-end against a running bridge on :8765
node tests\extension\test_end_to_end.js
```

The `tests\extension\*` suites launch a real headless Chrome and load the real unpacked extension — they are not mocks. `test_end_to_end.js` needs the Hub running, and reads the pairing code without printing it.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No pairing code set` | Click **Pair this extension** in the extension popup |
| Approval prompt never appeared | Open `127.0.0.1:8787` and pair again — the prompt shows there |
| `Invalid bridge token` | The code was rotated in *Settings → Connections*; pair again |
| `Chrome does not allow page access on chrome://…` | Chrome blocks extensions on its own pages — switch to a normal tab |
| Popup says *Unavailable* | `chrome://extensions` → **Reload** the extension (editing `background.js` does not re-register the worker) |
| Hub says "already running" | Another `start-hub.ps1` owns `:8787`; use it, or close that window first |

Unpair with extension **Options → Reset to Defaults**. Worker logs: `chrome://extensions` → ClarityOS → **service worker**.

---

## Upgrading from the previous name

The project used to ship under a different codename. Nothing needs doing by hand:

- **Settings, credentials, pairing tokens and the Hub database** are copied into `%APPDATA%\ClarityOS` on first launch. The old folder is left untouched, so you can roll back.
- **API keys in Windows Credential Manager** are still read from the old service name when no new entry exists.
- **Environment variables** renamed to `CLARITYOS_*`. Legacy names in `backend/.env` still work — they are mapped at startup, with a one-line notice.

---

## License

MIT
