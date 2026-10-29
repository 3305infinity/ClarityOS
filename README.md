# ClarityOS
**AI desktop assistant for Windows** — a docked sidebar or a transparent glass-pill overlay, driven by voice and text, that controls your PC.
Built with Electron + Python. Thinks with Gemini. Acts through a local Hub, a Windows helper, and a Chrome extension.
---


---


## Features
- **Two presentation modes** — a **Docked Sidebar** for longer conversations, or a **Floating Assistant** pill for quick requests. Pick one from the tray; the choice is remembered across restarts
- **Voice-first** — press `Ctrl+Shift+Space`, speak naturally, watch it act
- **SPAV agent** — Sense → Plan → Act → Verify, with milestone-based execution
- **Approval-gated** — every consequential tool call goes through a policy engine and an approval prompt
- **Browser automation** — a Chrome extension bridges real tabs (read pages, find elements, click, type, extract data)
- **Local Hub** — a dashboard at `127.0.0.1:8787` for conversation, activity, settings and connections
- **Multi-modal responses** — cards, tables, rich text, step timelines, image viewer
- **Platform-aware planning** — plans target Windows shells and apps, not a borrowed macOS environment
- **LangGraph (opt-in)** — set `MOONWALK_USE_LANGGRAPH=1` to run the graph agent instead of the linear loop
---


## Quick Start
```powershell
# 1. Install Node dependencies
npm install


# 2. Python environment (creates backend\.venv and installs requirements)
bash setup.sh          # Git Bash
# or run the helper script
.\start-hub.ps1


# 3. Configure
copy backend\.env.example backend\.env
# Fill in GOOGLE_API_KEY (or add it later in the app: Settings → API keys)
# A key saved in Settings takes precedence over backend\.env — see below


# 4. Launch
npm start
```

The docked sidebar appears on first launch and the backend starts with it.
Local voice (Windows SAPI) needs no API key; Gemini does.


### Presentation modes

ClarityOS can present itself two ways, and the choice is a stored setting
rather than a side effect of how it started:

| Mode | What it is |
|------|------------|
| **Docked Sidebar** | A persistent panel docked to the screen edge. The default. Collapsible to a narrow rail; its width is remembered |
| **Floating Assistant** | The compact, draggable glass pill that floats over your desktop |

Switch from the tray menu — the two modes are radio items at the top. Switching
reuses the same running app, the same backend and the same conversation; it
does not restart anything. The selection is saved in `sidebar-state.json` under
your Electron `userData` folder, so it survives restarts.


### Keyboard shortcuts


| Shortcut | Action |
|----------|--------|
| `Ctrl+Shift+Space` / `Alt+Space` | Activate voice input |
| `Ctrl+Shift+A` | Show / hide the sidebar |
| `Esc` | Dismiss the overlay, stop speech, or cancel the running task |
| `Ctrl+Shift+Q` | Quit ClarityOS |

Neither window has a taskbar entry, so ClarityOS also lives in the system tray:
**Docked sidebar / Floating assistant / Show / Hide / Reset position / Clear
saved Gemini key / Quit**. Drag the pill to move it; its position is remembered.

Closing or hiding a window does not stop the backend — quit explicitly with
`Ctrl+Shift+Q` or the tray.


### Ports


| Port | What |
|------|------|
| `8787` | Hub dashboard + API (HTTP/WS) |
| `8765` | Chrome extension bridge (WS) |
| `8000` | Electron voice server (WS) |


---


## Chrome Extension

The **ClarityOS Browser Bridge** gives the agent real tabs to work with.

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → select `chrome_extension\`
2. Click the ClarityOS icon in the toolbar → **Pair this extension**
3. Click **Approve** in the ClarityOS window

That's it — one approval, once. The pairing code is provisioned over the
loopback bridge and reused across restarts; you never copy a secret by hand.
Pasting the code from *Settings → Connections* into the extension's Options
still works as a fallback.


ClarityOS will never pair an extension you did not approve: the request is
deny-by-default, expires after two minutes, and the prompt names who is asking.


**Troubleshooting**

| Symptom | Fix |
|---|---|
| `No pairing code set` | Click **Pair this extension** in the popup |
| Approval prompt never appeared | Open the Hub at `127.0.0.1:8787` and pair again — the prompt shows there |
| `Invalid bridge token` | The code was rotated in *Settings → Connections*; pair again |
| `Chrome does not allow page access on chrome://…` | Chrome blocks extensions on its own pages — switch to a normal site tab |
| Popup says *Unavailable* | `chrome://extensions` → **Reload** the extension (editing `background.js` does not re-register the worker) |

Unpair with extension **Options → Reset to Defaults**. Worker logs:
`chrome://extensions` → ClarityOS → **service worker**.

---

## Gemini API key

The key can come from two places, and **a key saved in the app wins**:

1. **Settings → API keys** — stored encrypted in `credentials.enc` (Electron `safeStorage`)
2. **`backend/.env`** — `GOOGLE_API_KEY`

That precedence is the usual cause of "the key in `.env` is correct but
ClarityOS says it was rejected": a stale or truncated key is sitting in
Settings and shadowing it. Obviously-invalid stored keys (too short to be real)
are ignored automatically and logged, but a well-formed wrong key is not.

To fall back to `.env`, use the tray item **Clear saved Gemini key**. It
removes only the key, leaving the rest of your credentials intact.

Provider failures are reported by kind, and ClarityOS does not retry the ones
that cannot succeed:

| Failure | Behaviour |
|---|---|
| `401` / `403` / invalid key | Reported as an auth problem; no retry |
| `429 RESOURCE_EXHAUSTED` | Quota is gone; no retry against the exhausted model |
| `503 UNAVAILABLE`, network | Transient; at most one retry, then it stops and says so |

Keys are never written to logs, prompts, or the UI.

---

## Architecture
```
Sidebar / overlay ──WS :8000──►  Voice server  ──►  Agent  ──►  Tool registry
   (one at a time)                                     │              │
Hub dashboard     ──WS :8787──►  Hub  ◄── policy + approvals ────────┘
                                    │
Chrome extension  ──WS :8765──►  Browser bridge  (tabs, DOM, forms)
Windows helper    ──WS :8787/helper──►  native actions
```

The sidebar and the overlay are two views of one assistant, not two assistants:
they speak the same WebSocket protocol to the same voice server, and only the
active presentation holds a connection. The voice server builds one agent per
connection, so this is what keeps a single request from being handled twice.

Every tool call is classified by `backend/hub/policy.py` and anything
consequential — shell, file writes, purchases, submits — needs an explicit
approval before it runs.

### Agent loop (SPAV)

---

## Project Structure

```
ClarityOS/
├── main.js                  # Electron main: windows, presentation mode, tray, backend lifecycle
├── preload.js               # Overlay IPC bridge (auth, credentials, mouse, mode, quit)
├── setup.sh                 # Python venv setup
├── start-hub.ps1            # Start the Hub on Windows
│
├── renderer/                # Floating Assistant — glass-pill overlay (HTML/CSS/JS)
├── sidebar/                 # Docked Sidebar — same backend, same WS protocol
│   ├── sidebar.js           # WS client, voice input, conversation
│   └── preload.js           # Sidebar-only IPC (no tool access)
│
├── web_app/                 # Hub dashboard served at 127.0.0.1:8787
│   ├── index.html
│   ├── app.js               # WS client, approvals, settings, activity
│   ├── styles.css
│   └── shader-metal.js      # WebGL hero backdrop
│
├── backend/
│   ├── platform_profile.py  # OS detection for platform-aware planning
│   ├── agent/
│   │   ├── core_v2.py       # SPAV agent loop
│   │   ├── graph_agent.py   # LangGraph agent (opt-in)
│   │   ├── planner.py       # LLM task decomposition
│   │   ├── milestone_executor.py
│   │   ├── perception.py / verifier.py / memory.py
│   ├── hub/
│   │   ├── app.py           # FastAPI Hub: REST, WS, bridge startup
│   │   ├── core.py          # Hub state, clients, approvals
│   │   ├── policy.py        # Capability classification + approval rules
│   │   └── approval.py      # Single-use approval nonces
│   ├── browser/
│   │   ├── bridge.py        # Extension connection + action queue
│   │   └── pairing.py       # Approval-gated pairing broker
│   ├── servers/
│   │   ├── local_server.py  # Electron voice server (:8000)
│   │   ├── browser_bridge_server.py  # Extension bridge (:8765)
│   │   └── voice_guard.py   # Policy guard on the voice tool path
│   ├── voice/               # STT/TTS, progress narration
│   └── tools/               # browser_*, file, form, cloud, vault tools
│
├── chrome_extension/        # MV3: background worker, content script, popup, options
├── windows_helper/          # Native Windows action helper
├── deploy/appengine/        # Public marketing site
├── docs/                    # GUIDE.md, ARCHITECTURE.md, LANGGRAPH.md
└── tests/                   # Python + JS test suites
```

---

## Testing

```powershell
# Python: hub, policy, browser bridge, pairing
backend\.venv\Scripts\python.exe -m pytest tests\ -q --asyncio-mode=auto

# Pairing (real WebSocket server + real protocol)
backend\.venv\Scripts\python.exe -m pytest tests\browser -q --asyncio-mode=auto

# Chrome extension: worker message contract (real background.js, mocked chrome APIs)
node tests\extension\test_worker_messages.js

# Chrome extension: real Chrome, real Options UI, real worker restart
node tests\extension\test_pairing_roundtrip.js

# Chrome extension end-to-end against a running ClarityOS bridge on :8765
node tests\extension\test_end_to_end.js
```

The `tests\extension\*` suites launch a real headless Chrome and load the real
unpacked extension — they are not mocks. `test_end_to_end.js` requires the Hub
to be running and reads the pairing code from `%APPDATA%\Moonwalk\tokens.json`
without printing it.

---

## Cloud Deployment

Deploy the brain to GCP Cloud Run for multi-user, always-on operation:

```bash
docker build --platform linux/amd64 -t $IMAGE .
docker push $IMAGE

gcloud run deploy clarityos-brain \
  --image $IMAGE --region us-central1 --allow-unauthenticated \
  --port 8080 --cpu 2 --memory 2Gi \
  --set-env-vars "GEMINI_API_KEY=<key>,AUTH_SHARED_SECRET=<secret>"
```

Health check: `GET /health` → `{"status":"ok","agents":0}`

See [docs/GUIDE.md](docs/GUIDE.md) for the full deployment guide.

---

## Security Notes

- Random per-install tokens (ui / helper / bridge) in `%APPDATA%\Moonwalk\tokens.json` —
  no default or hard-coded secrets.
- `Host` and `Origin` are validated on every API call and WebSocket
  (DNS-rebinding and cross-site WebSocket defence); the extension bridge accepts
  only `chrome-extension://` origins.
- Pairing is deny-by-default and needs a human click; the code is never logged,
  printed, or exposed in diagnostics.
- API keys are stored with Windows `safeStorage` / Credential Manager, never in
  plain text in the repo. `backend/.env.example` documents names only.
- Reading your active editor file excludes sensitive paths by default
  (`.env*`, private keys, `*credentials*`, `*secret*`, `.npmrc`, `.netrc`, …)
  and redacts secret-shaped lines from anything it does read, so file contents
  are filtered before they can reach the model.
- Retrieved content is validated before it is summarised, and a URL is only
  presented as a page's link when that destination was actually extracted from
  the page — never recalled from the model.

---

## License

MIT

---

<p align="center">
  <strong>ClarityOS</strong> — Your AI copilot for Windows<br/>
  <sub>Voice-first · Sidebar or floating · SPAV agent · Approval-gated</sub>
</p>
