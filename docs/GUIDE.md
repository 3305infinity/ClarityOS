# Moonwalk — Complete Guide

> **Everything in one place** — from what Moonwalk is and how it was built, to how customers install it and how you run, manage, and ship it.

---

## Table of Contents

### For Customers
1. [What is Moonwalk?](#1-what-is-moonwalk)
2. [Installing Moonwalk](#2-installing-moonwalk)
3. [Installing the Chrome Extension](#3-installing-the-chrome-extension)
4. [Using Moonwalk (Daily Use)](#4-using-moonwalk-daily-use)
5. [Starting & Stopping the App](#5-starting--stopping-the-app)
6. [Troubleshooting](#6-troubleshooting)

### For Developers & Operators
7. [Running the Development Server](#7-running-the-development-server)
8. [Environment Variables](#8-environment-variables)
9. [Project Structure](#9-project-structure)
10. [How Moonwalk Works (Architecture)](#10-how-moonwalk-works-architecture)
11. [The SPAV Agent Loop Explained](#11-the-spav-agent-loop-explained)
12. [How Google Cloud is Used](#12-how-google-cloud-is-used)
13. [How the Software Was Built](#13-how-the-software-was-built)
14. [Building a DMG for Distribution](#14-building-a-dmg-for-distribution)
15. [Code Signing & Notarization](#15-code-signing--notarization)
16. [Distributing to Customers via GCS](#16-distributing-to-customers-via-gcs)
17. [Cloud Deployment (GCP Cloud Run)](#17-cloud-deployment-gcp-cloud-run)
18. [Running Tests](#18-running-tests)

---

# PART 1 — FOR CUSTOMERS

---

## 1. What is Moonwalk?

Moonwalk is an **AI desktop assistant for macOS**. It lives as a small floating glass pill at the top-centre of your screen and responds to your voice or typed commands. You speak to it naturally — *"open my emails"*, *"search for the best AirPods deal"*, *"write a reply to my last message"* — and it does the work on your Mac.

### What can it do?

- **Open and control apps** — launch, quit, switch between any Mac app
- **Control your Mac** — adjust volume, brightness, take screenshots, lock screen
- **Browse the web** — search, read pages, fill forms, extract information (needs Chrome extension)
- **Write content** — draft emails, messages, documents, summaries in any app
- **Answer questions** — with full reasoning, using your current screen as context
- **Multi-step tasks** — *"find the cheapest flight to Tokyo and open the booking page"*
- **Remember things** — the vault stores notes, preferences, and facts across sessions

### How does it hear me?

Moonwalk listens for the wake word **"Hey Moonwalk"** at all times using low-power on-device processing (Picovoice). When it hears you, the pill expands and shows your speech being transcribed in real-time. Alternatively, press a keyboard shortcut at any time.

---

## 2. Installing Moonwalk

### Step 1 — Download
You will receive a link to a file named `Moonwalk-x.x.x-universal.dmg`. Download it and double-click the `.dmg` file to open it.

### Step 2 — Install to Applications
A window opens showing the Moonwalk icon and an **Applications** folder shortcut. **Drag the Moonwalk icon into the Applications folder.** Close the window and eject the disk image (drag to Trash or right-click → Eject).

### Step 3 — Open Moonwalk
Open your **Applications** folder and double-click **Moonwalk**.

> ⚠️ **Gatekeeper warning (first time only):** macOS may say it *"cannot be opened because the developer cannot be verified."*
>
> **Fix:** Right-click the Moonwalk icon → **Open** → **Open** in the dialog. You only need to do this once.

### Step 4 — First-Launch Onboarding (~60 seconds)

The first time Moonwalk opens it shows a 3-step setup wizard:

**Step 1 — Enter your API key**
- You need a free **Gemini API key** from Google AI Studio
- Click the blue link in the wizard to open [aistudio.google.com](https://aistudio.google.com/app/apikey) — no credit card, free tier is plenty
- Paste the key into the first field and click **Save & Continue**
- Optionally add a **Picovoice access key** (free at [console.picovoice.ai](https://console.picovoice.ai)) to enable the "Hey Moonwalk" wake word

> ⚠️ **Without a Picovoice key the wake word is disabled.** Use `⌘⇧Space` instead — it works identically and requires no extra account.

**Step 2 — Automatic setup (~60 seconds first time)**
Moonwalk installs its Python environment automatically. You'll see three items go green:
- ✅ Python environment (installs dependencies, ~60s the first time)
- ✅ WebSocket connection
- ✅ Microphone access

Click **Continue** once all three are green.

**Step 3 — Shortcuts & Chrome Extension**
Shows keyboard shortcuts and buttons to install the Chrome browser extension.

### Step 5 — Grant Accessibility Permission

For Moonwalk to control your Mac (clicking buttons, typing in apps, reading your screen), it needs Accessibility access:

1. Open **System Settings** → **Privacy & Security** → **Accessibility**
2. Find **Moonwalk** in the list
3. Toggle it **ON**
4. Enter your Mac password if prompted

> Without this, Moonwalk can still answer questions and do web research, but cannot click or type in other apps.

---

## 3. Installing the Chrome Extension

The Chrome extension lets Moonwalk browse the web, read page content, fill forms, and extract information from websites. It is optional but strongly recommended.

### Option A — From the Moonwalk Setup Wizard (easiest)

During first launch, the setup wizard's second screen shows:

- **📥 Save Extension to Downloads** — click this. Moonwalk saves the extension folder to your Downloads and opens it in Finder automatically.

Then continue to **Step 3** below.

### Option B — From the Download Page

Download the `moonwalk-browser-bridge.zip` from your download page, then unzip it by double-clicking.

---

### Installing the Extension in Chrome

After getting the extension folder by either method:

1. Open **Google Chrome**
2. In the address bar type `chrome://extensions` and press **Enter**
3. Turn on **Developer mode** — toggle in the **top-right corner**
4. Click **"Load unpacked"** (appears top-left once Developer mode is on)
5. In the file picker, navigate to and **select the `moonwalk-browser-bridge` folder**
6. The **Moonwalk Browser Bridge** extension appears in your list ✓

### Pin the Extension (recommended)

1. Click the puzzle piece icon 🧩 in Chrome's toolbar
2. Click the pin 📌 next to **Moonwalk Browser Bridge**

The badge turns **green** when connected to the Moonwalk desktop app.

> **"Developer mode" banner:** Chrome shows a warning banner at the top. You can dismiss it. It reappears occasionally — this is a Chrome limitation for extensions loaded outside the Web Store. It is safe to ignore.

---

## 4. Using Moonwalk (Daily Use)

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `⌘ Shift Space` | Activate voice input — pill expands and listens |
| `⌥ Space` (Option+Space) | Open the text command panel — type a command |
| `Esc` | Dismiss the overlay or cancel the current action |

### Voice Commands

Press `⌘ Shift Space` (or say *"Hey Moonwalk"* if the wake word is active) and speak naturally:

```
"Open Spotify"
"Search for the best noise-cancelling headphones under $200"
"What's on my screen right now?"
"Write a follow-up to my last email"
"Open YouTube and search for lo-fi music"
"Turn the volume down to 40%"
"What time is it in London?"
"Take a screenshot"
"Remember that my flight is on March 22nd"
"Close all Safari windows"
```

### Text Commands

Press `⌥ Space` to open a floating text box. Type your command and press **Enter** or click the arrow button. Useful in quiet environments or when you need precise phrasing.

### Response Cards

After completing a task, Moonwalk shows a **response card** which can contain:
- Plain text summaries
- Formatted tables and lists
- Math equations (rendered with KaTeX)
- Code blocks with syntax highlighting
- Step timelines showing exactly what actions were taken

Press `Esc` or click outside the card to dismiss it.

### Plan Preview

For complex multi-step tasks, Moonwalk shows a **plan modal** first — a numbered list of what it intends to do. You can:
- Click **Proceed** to execute the plan
- Click **Cancel** to abort

---

## 5. Starting & Stopping the App

### Starting Moonwalk

- Open **Moonwalk** from your Applications folder
- Or click the Moonwalk icon in your Dock (if pinned)

The glass pill appears at the top-centre of your screen within 2–3 seconds. On the very first launch, allow ~60 seconds for the Python setup.

### Stopping / Quitting Moonwalk

Moonwalk is an overlay with no traditional window or menu bar. To quit:

| Method | How |
|--------|-----|
| Dock | Right-click the Moonwalk dock icon → **Quit** |
| Keyboard | Press `⌘ Q` while Moonwalk is the active app |
| Force quit | Press `⌘ Option Esc` → select Moonwalk → Force Quit |
| Activity Monitor | Open Activity Monitor, search for "Moonwalk" or "Python", force quit both |

When the Electron window quits, the Python backend process (running on port 8000) is automatically stopped with it.

### Restarting

Simply open Moonwalk again. It reconnects to any existing session and resumes conversation history from where you left off (up to 30 minutes of idle time before history clears).

---

## 6. Troubleshooting

| Problem | Solution |
|---------|----------|
| Glass pill doesn't appear | Check Activity Monitor — Moonwalk may be on another workspace. Try pressing `⌘ Shift Space`. |
| "Hey Moonwalk" not responding | Wake word requires a Picovoice key. Use `⌘⇧Space` instead, or add the key on first launch / in `backend/.env` (dev) |
| Want to change the API key | Delete `~/Library/Application Support/moonwalk/credentials.enc` and relaunch |
| App says "Backend not ready" | Wait 60 seconds on first launch. If it persists: quit and reopen. |
| "Cannot be opened" security error | Right-click the app → Open → Open. One-time only. |
| Chrome extension shows red badge | Make sure the Moonwalk app is running first, then click the extension badge to reconnect |
| Accessibility actions don't work | **System Settings → Privacy & Security → Accessibility** — toggle Moonwalk ON |
| Slow to respond on complex tasks | Normal — the AI is planning and executing multiple steps. Simple tasks (open app, volume) respond in under 2 seconds. |
| No spoken voice response | TTS requires Google Cloud credentials. Responses still appear as text cards. |
| `xattr` error on startup | Run: `xattr -cr /Applications/Moonwalk.app` in Terminal, then reopen |

---

# PART 2 — FOR DEVELOPERS & OPERATORS

---

## 7. Running the Development Server

### Prerequisites

| Tool | Min Version | Install |
|------|-------------|---------|
| Node.js | 18 | `brew install node` |
| Python | 3.10+ | `brew install python@3.13` |
| npm | 9+ | bundled with Node |

> ⚠️ **Python version matters.** The codebase uses `str | None` union type syntax which requires **Python 3.10+**. The system Python on macOS is 3.9 — always use a Homebrew or pyenv Python.

### First-Time Setup

```bash
# 1. Clone the repo
git clone https://github.com/<your-org>/Moonwalk.git
cd Moonwalk

# 2. Install Node dependencies
npm install

# 3. Create the Python virtual environment with Python 3.10+
python3.13 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r backend/requirements.txt

# 4. Set up environment variables
cp backend/.env.example backend/.env
nano backend/.env      # fill in GEMINI_API_KEY at minimum
```

### Starting the App

```bash
npm start
```

This does three things:
1. Launches the **Electron overlay** (glass pill UI)
2. Spawns the **Python backend** (`backend/servers/local_server.py`) on `ws://127.0.0.1:8000`
3. Starts the **browser bridge** (`backend/servers/browser_bridge_server.py`) on `ws://127.0.0.1:8765`

Expected output:
```
[Backend] Starting Python server...
[Python] Server running on ws://127.0.0.1:8000
Browser bridge running on ws://127.0.0.1:8765
[Backend] READY
[Python] Electron App Connected!
[Python] [Server] Agent initialized: V2
[Python] Porcupine initialized with CUSTOM wake word: 'Hey Moonwalk'
```

### Stopping the App

Press `Ctrl+C` in the terminal. Electron exits, then sends `SIGTERM` to Python. Both processes shut down cleanly.

### Running the Python Backend Standalone

Useful for testing agent logic without the Electron UI:

```bash
source venv/bin/activate
python backend/servers/local_server.py
```

The backend starts on `ws://127.0.0.1:8000`. Connect with any WebSocket client (e.g. `wscat -c ws://127.0.0.1:8000/ws`) and send JSON.

### Running the Browser Bridge Standalone

```bash
source venv/bin/activate
python backend/servers/browser_bridge_server.py
# Starts on ws://127.0.0.1:8765
```

---

## 8. Environment Variables

All variables are loaded from `backend/.env`. Copy the example to get started:

```bash
cp backend/.env.example backend/.env
```

### Required

| Variable | Description | Where to get it |
|----------|-------------|-----------------|
| `GEMINI_API_KEY` | Google Gemini API key — the AI brain | [aistudio.google.com](https://aistudio.google.com) |

### Recommended

| Variable | Description | Where to get it |
|----------|-------------|-----------------|
| `PICOVOICE_ACCESS_KEY` | Wake word detection ("Hey Moonwalk"). **Without this the wake word is fully disabled** — use `⌘⇧Space` instead. | [console.picovoice.ai](https://console.picovoice.ai) — free tier available |

### Optional — Model Overrides

Defaults are defined in `backend/providers/models.py` (the single source of truth).
An empty or whitespace-only override is ignored and the default applies.

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_FAST_MODEL` | `gemini-3.5-flash` | Simple/fast single-step tasks |
| `GEMINI_POWERFUL_MODEL` | `gemini-3.5-flash` | Complex multi-step reasoning |
| `GEMINI_ROUTING_MODEL` | `gemini-3.5-flash` | Classifies requests (FAST vs POWERFUL) |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash` | Emergency fallback on POWERFUL failure |

**Model aliases.** These public ids are served by a dated family member, and quota
is billed to *that* model. `gemini-3.5-flash` is billed to `gemini-3.6-flash`, so a
429 can name a model you never configured — that is expected, not a misconfiguration.
The first successful call logs `… is served by … (alias)`.

**Free-tier quota.** 20 generate-content requests per day, **per project per model**.
A new API key in the same Google AI Studio project does *not* reset it. When one
family is exhausted, point a tier at another (each family has its own bucket):

```env
GEMINI_FAST_MODEL=gemini-3.1-flash-lite
```

Moonwalk records the exhausted model and stops calling it through the FAST,
POWERFUL and FALLBACK tiers until the quota resets, so you get one clear message
instead of repeated 429s.

### Optional — Cloud Mode

| Variable | Description |
|----------|-------------|
| `MOONWALK_CLOUD_URL` | WebSocket URL of Cloud Run brain (enables cloud mode via `mac_client.py`) |
| `MOONWALK_CLOUD_TOKEN` | Auth token matching the Cloud Run `AUTH_SHARED_SECRET` |
| `GCP_PROJECT` | Google Cloud project ID |
| `MOONWALK_GCS_BUCKET` | GCS bucket name for memory storage |

### Optional — Voice / TTS

| Variable | Default | Description |
|----------|---------|-------------|
| `MOONWALK_TTS_VOICE` | `en-US-Neural2-J` | Google Cloud TTS Neural2 voice |
| `MOONWALK_TTS_SPEED` | `1.05` | Speaking rate (1.0 = normal speed) |

### Optional — Ports

| Variable | Default | Description |
|----------|---------|-------------|
| `MOONWALK_BACKEND_PORT` | `8000` | Main Python WebSocket server port |
| `MOONWALK_BROWSER_BRIDGE_PORT` | `8765` | Chrome extension bridge port |

---

## 9. Project Structure

```
Moonwalk/
├── main.js                      # Electron main process — window, IPC, Python lifecycle
├── preload.js                   # contextBridge IPC (auth, credentials, extension export)
├── package.json                 # npm scripts, electron-builder config
├── setup.sh                     # Auto-runs on first launch: creates venv, installs deps
├── hey_moonwalk.ppn             # Picovoice custom wake word model ("Hey Moonwalk")
│
├── renderer/
│   ├── index.html               # Glass-pill overlay markup + all modal scaffolding
│   ├── styles.css               # Full UI (glassmorphism, state animations, modals)
│   └── renderer.js              # UI state machine, WebSocket client, audio capture
│
├── backend/
│   ├── servers/
│   │   ├── local_server.py      # Main local server: audio + wake word + agent
│   │   ├── cloud_server.py      # Cloud Run server: agent brain only (no audio/tools)
│   │   ├── mac_client.py        # Local client for cloud mode: audio + tools, no AI
│   │   └── browser_bridge_server.py  # WebSocket server for Chrome extension
│   │
│   ├── agent/
│   │   ├── core_v2.py           # SPAV agent loop — the main brain
│   │   ├── planner.py           # Milestone & step data types (dataclasses)
│   │   ├── task_planner.py      # LLM-powered milestone plan generation
│   │   ├── milestone_executor.py  # LLM micro-loop executor per milestone
│   │   ├── perception.py        # 3-layer context capture (AppleScript + DOM + Vision)
│   │   ├── verifier.py          # Post-action verification engine
│   │   ├── world_state.py       # Typed desktop context + intent classification
│   │   ├── memory.py            # Local memory (conversation, vault, profile, tasks)
│   │   ├── cloud_memory.py      # Cloud memory (Firestore + GCS, same API)
│   │   ├── rag.py               # RAG engine (Gemini embeddings + Firestore vector search)
│   │   ├── glance.py            # Parallel screen perception
│   │   └── constants.py         # Shared tool classification sets
│   │
│   ├── browser/
│   │   ├── bridge.py            # Browser bridge state and action queue
│   │   ├── interpreter_ai.py    # AI-driven action interpretation
│   │   ├── listing_extractor.py # Structured data extraction from web pages
│   │   ├── resolver.py          # Browser state resolution
│   │   └── selector_ai.py       # AI-powered DOM element selection
│   │
│   ├── providers/
│   │   ├── gemini.py            # Google Gemini API client (google-genai SDK)
│   │   ├── router.py            # 4-tier model router (FAST / POWERFUL / ROUTING / FALLBACK)
│   │   └── base.py              # LLMProvider abstract interface
│   │
│   ├── tools/
│   │   ├── mac_tools.py         # macOS GUI control (click, type, open app, screenshot)
│   │   ├── browser_aci.py       # High-level compound browser tools (ACI layer)
│   │   ├── browser_tools.py     # Raw browser DOM tools
│   │   ├── file_tools.py        # File read/write/search
│   │   ├── gworkspace_tools.py  # Google Workspace (Gmail, Drive, Docs, Calendar)
│   │   ├── vault_tools.py       # Vault memory tools
│   │   ├── cloud_tools.py       # Cloud-only stub tools
│   │   └── registry.py          # Global tool registry + @registry.tool decorator
│   │
│   ├── multi_agent/
│   │   ├── sub_agent_manager.py # Parallel milestone dispatch across sub-agents
│   │   └── remote_executor.py   # Remote milestone execution
│   │
│   ├── voice/
│   │   └── tts.py               # Streamed Google Cloud TTS (Neural2, OGG/Opus)
│   │
│   ├── auth.py                  # Dual-mode auth (GCP ID token + shared secret)
│   ├── runtime_state.py         # Per-user session state registry
│   └── requirements.txt         # Python dependencies
│
├── chrome_extension/
│   ├── manifest.json            # MV3 manifest
│   ├── background.js            # Service worker: WebSocket bridge + keepalive
│   ├── content_script.js        # Injected in every page: DOM reader + snapshots
│   ├── Readability.js           # Mozilla article parser (bundled)
│   ├── popup.html / popup.js    # Extension popup: connection status display
│   └── options.html / options.js  # Settings: bridge URL + auth token
│
├── build/
│   ├── entitlements.mac.plist   # macOS hardened runtime entitlements
│   └── notarize.cjs             # Apple notarization afterSign hook
│
├── scripts/
│   ├── release.sh               # Full release pipeline: package → build → upload
│   ├── upload-gcs.mjs           # Upload DMG + extension zip to GCS
│   └── package-extension.mjs    # Package Chrome extension into customer zip
│
└── deploy/
    └── deploy_gcp.sh            # One-command GCP Cloud Run deployment script
```

---

## 10. How Moonwalk Works (Architecture)

Moonwalk is split into three layers that communicate over WebSockets:

```
┌─────────────────────────────────────────────────────────────┐
│  ELECTRON  (Chromium renderer process)                      │
│  Glass pill UI · State machine · Audio capture (PCM)       │
│  WebSocket client → ws://127.0.0.1:8000/ws                 │
└────────────────────────┬────────────────────────────────────┘
                         │  JSON messages over WebSocket
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  PYTHON BACKEND  (local_server.py)                          │
│  Wake word (Picovoice) · STT (Google) · SPAV Agent V2      │
│  macOS tools · Memory · TTS streaming                       │
└────────────────────────┬────────────────────────────────────┘
                         │  JSON messages over WebSocket
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  CHROME EXTENSION  (background.js + content_script.js)      │
│  DOM snapshots · Page interaction · Form fill               │
│  ws://127.0.0.1:8765 ←→ browser_bridge_server.py           │
└─────────────────────────────────────────────────────────────┘
```

### Complete Data Flow — a voice command end-to-end

```
User: "Hey Moonwalk, search Amazon for AirPods"
         │
         ▼
Electron renderer.js
  → Raw PCM audio chunks captured from mic via Web Audio API
  → Streamed to Python as base64 over WebSocket
         │
         ▼
Python local_server.py
  → pvporcupine detects wake word "Hey Moonwalk" in audio stream
  → SpeechRecognition + Google STT transcribes the rest
  → Sends {type: "transcription", text: "search Amazon for AirPods"}
         │
         ▼
MoonwalkAgentV2 (core_v2.py)
  → L1 Perception: active app + window title via AppleScript (~50ms)
  → L2 Perception: DOM snapshot from Chrome extension (~200ms)
  → Router classifies: POWERFUL model (multi-step browser task)
         │
         ▼
TaskPlanner (task_planner.py)
  → Gemini Pro generates a MilestonePlan:
    Milestone 1: Open Amazon and search for AirPods
    Milestone 2: Extract and return the top results
         │
         ▼
SubAgentManager (multi_agent/)
  → Dispatches milestones to executor(s)
         │
         ▼
MilestoneExecutor (milestone_executor.py) — LLM micro-loop:
  → action 1: open_url("https://www.amazon.com/s?k=AirPods")
  → Verifier: URL opened? ✓
  → action 2: get_web_information() — reads page via Chrome extension
  → Verifier: results received? ✓
  → Milestone 1 COMPLETE → Milestone 2 starts
  → action 3: send_response({results table})
         │
         ▼
Electron renderer.js
  → Receives response card payload
  → Renders results table in the overlay
  →  Optional: plays TTS audio (streamed OGG/Opus chunks from Google TTS)
```

### WebSocket Message Protocol

| Direction | Message type | Meaning |
|-----------|-------------|---------|
| Server → Renderer | `state-idle` | Show idle glass pill |
| Server → Renderer | `state-listening` | Show listening pill with typewriter transcription |
| Server → Renderer | `state-loading` | Show thinking dots |
| Server → Renderer | `state-doing` | Show spinner + action text |
| Server → Renderer | `response` | Final response card (Markdown / table / text) |
| Server → Renderer | `thinking` | Visible thought bubble during planning |
| Server → Renderer | `await_reply` | Keep mic open for follow-up reply |
| Server → Renderer | `tts_chunk` | Streamed audio chunk (base64 OGG/Opus) |
| Renderer → Server | `audio_data` | Raw PCM audio chunk (base64) |
| Renderer → Server | `user_action` | Button press: `approve_plan` / `cancel_plan` |
| Renderer → Server | `text_command` | Text typed in command panel |

---

## 11. The SPAV Agent Loop Explained

The core AI engine uses **SPAV**: **S**ense → **P**lan → **A**ct → **V**erify. This is implemented in `backend/agent/core_v2.py`.

### Stage 1 — Sense (Perception)

Three layers run in parallel before every LLM call:

| Layer | Method | What it captures | Latency |
|-------|--------|-----------------|---------|
| L1 | AppleScript (`osascript`) | Active app name, window title, browser URL | ~50ms |
| L2 | Chrome extension DOM snapshot | Page text, selected text, element refs | ~200ms |
| L3 | Gemini Vision (screenshot) | Full visual understanding of screen | ~1s (on demand only) |

The combined `ContextSnapshot` is serialised into a compact structured block and injected into every LLM system prompt so the model always knows exactly what's on screen.

### Stage 2 — Plan (Milestone Planning)

`TaskPlanner` calls Gemini Pro with the user's intent + perceived context and generates a `MilestonePlan` — a list of `Milestone` dataclasses:

```python
@dataclass
class Milestone:
    id: int
    goal: str            # "What must be accomplished" (natural language outcome)
    success_signal: str  # "What observable evidence proves it is done"
    hint_tools: List[str]  # Suggested tool categories (advisory, not prescriptive)
```

This is the core design insight — the planner specifies **what** to achieve, never **how**. The executor figures out the how. This makes the system far more robust than step-by-step scripts.

### Stage 3 — Act (Milestone Executor)

Each milestone runs its own LLM micro-loop in `MilestoneExecutor`:

1. Show the LLM: milestone goal + success signal + available tools + results so far
2. LLM picks the next tool to call (or declares the milestone done)
3. Tool executes (macOS API / Chrome extension / web)
4. Result fed back to LLM
5. Repeat until the success signal is satisfied or the safety cap (50 actions) is hit

Independent milestone groups are dispatched **in parallel** by `SubAgentManager` to save time on multi-part tasks.

### Stage 4 — Verify

After every tool call, `ToolVerifier` checks:
- Tool output for error keywords and failure patterns
- For UI-mutating actions (click, type): re-reads the screen to confirm the action had effect
- High-confidence deterministic actions (e.g. `open_url`) get a fast-path skip to avoid wasted latency
- If verification fails, the executor can retry with a suggested fix from the verifier

### Model Routing

The `ModelRouter` classifies every incoming request before execution and picks one of four tiers:

| Tier | Model | When used |
|------|-------|-----------|
| ROUTER | `gemini-2.5-flash` | Classifies every request (FAST vs POWERFUL) |
| FAST | `gemini-3-flash-preview` | Open app, volume, brightness, one-liner facts |
| POWERFUL | `gemini-3.1-pro-preview-customtools` | Everything complex, multi-step, or browser-related |
| FALLBACK | `gemini-2.5-pro` | Emergency fallback on POWERFUL model failure |

Only ~5–10% of requests ever go to FAST. The router is deliberately biased toward POWERFUL — routing a simple task there costs cents more, but routing a complex task to FAST causes failure.

---

## 12. How Google Cloud is Used

Moonwalk uses five Google Cloud services across the full stack:

### 1. Gemini API — the AI brain

Every intelligent decision goes through **Google Gemini** via the `google-genai` Python SDK (`backend/providers/gemini.py`):

- **Gemini 2.5 Flash** — fast classifier (routes requests in ~500ms)
- **Gemini 3 Flash Preview** — simple task execution (open app, set volume)
- **Gemini 3.1 Pro Preview** — complex reasoning, browser tasks, multimodal, native function calling
- **Gemini 2.5 Pro** — emergency fallback

The Pro model uses **native function calling** — it returns structured JSON (`{tool_name, args}`) rather than free text, so the executor can directly call the right tool without any text parsing.

### 2. Google Cloud Text-to-Speech — voice output

Spoken responses use **Google Neural2** voices via `google-cloud-texttospeech` SDK (`backend/voice/tts.py`):

- The response is split into sentences
- All sentences are synthesized **concurrently** (parallel API calls)
- Audio chunks are yielded in order as they finish — the first sentence plays while the rest are still being generated, creating a seamless streaming feel
- Output format: OGG/Opus (plays natively in Chromium without any decoder library)
- Default voice: `en-US-Neural2-J` (natural, conversational)

### 3. Cloud Run — the cloud brain (production mode)

In cloud deployment mode, the SPAV agent runs on **Google Cloud Run** (`backend/servers/cloud_server.py`):

```
Your Mac:                          Google Cloud:
  mac_client.py  ←WebSocket→  Cloud Run (cloud_server.py)
  ├── Audio capture                ├── SPAV Agent V2
  ├── Wake word detection          ├── Gemini API calls
  ├── Speech-to-text               ├── Firestore reads/writes
  └── macOS tool execution ←─────── Tool execution requests
```

The Mac client handles everything that requires macOS APIs. The cloud handles all AI reasoning. This split means:
- The AI scales independently (Cloud Run scales to 0 when idle)
- macOS operations stay local and private
- Multiple users can share one cloud brain

### 4. Firestore — persistent cloud memory

When running in cloud mode, local file-backed memory is replaced with **Firestore** equivalents (`backend/agent/cloud_memory.py`):

| Local storage (dev) | Firestore path (cloud) | What it holds |
|--------------------|----------------------|---------------|
| `~/.moonwalk/sessions/*.json` | `users/{uid}/sessions/{sid}` | Conversation history (last 20 turns) |
| `~/.moonwalk/vault/*.json` | `users/{uid}/vault/{entry_id}` | Permanent vault memories |
| In-memory dict | `users/{uid}/profile` | Auto-extracted user profile (name, prefs, habits) |
| In-memory dict | `users/{uid}/tasks/{task_id}` | Background recurring tasks |

Large blobs (screenshots, documents over 1 MB) are automatically offloaded from Firestore to **Google Cloud Storage** at `gs://{bucket}/vault/`.

### 5. Gemini Embeddings + Firestore Vector Search — semantic memory (RAG)

The vault supports **semantic recall** via RAG (`backend/agent/rag.py`):

- Uses `text-embedding-004` model (768-dimensional vectors) via the Gemini API
- Vault entries are embedded when stored; embeddings are saved alongside them in Firestore
- Firestore's **native vector index** enables approximate nearest-neighbour search
- When Moonwalk needs to recall relevant memories, it embeds the query and finds semantically similar vault entries
- This lets Moonwalk "remember" your preferences, addresses, passwords (encrypted), recurring tasks etc.

### Infrastructure Summary

```
Your Mac
  ├── Electron overlay (UI, audio, local tools)
  └── mac_client.py / local_server.py
          │  WebSocket (WSS encrypted)
          ▼
GCP Cloud Run  (moonwalk-brain)
  ├── SPAV Agent V2
  ├── Gemini API ──────────────────────────► Google AI Studio
  ├── Google Cloud TTS ────────────────────► Neural2 voice synthesis
  └── Memory layer
        ├── Firestore ◄──── conversations, vault, profile, tasks
        └── Cloud Storage ◄── large blobs (>1 MB)
              └── Firestore vector index ◄── RAG semantic search
```

---

## 13. How the Software Was Built

### Technology Stack

| Layer | Technology | Why chosen |
|-------|-----------|-----------|
| Desktop shell | **Electron 36** | True OS-level transparent overlay; `setIgnoreMouseEvents` for click-through; Chromium renderer for CSS glassmorphism + KaTeX math |
| UI | **Vanilla JS + CSS** | No framework overhead; direct DOM manipulation for 60fps state transitions |
| Backend | **Python 3.13 + asyncio + websockets** | Picovoice SDK is Python-only; `pyobjc` is the gold standard for macOS Accessibility API; `asyncio` gives clean concurrent I/O |
| AI provider | **Google Gemini** | Native function calling; multimodal; same credentials for TTS + embeddings + cloud; fastest Flash model for routing |
| macOS control | **AppleScript + pyobjc (Quartz/AppKit)** | AppleScript covers 90% of app control; Quartz Accessibility API handles the rest (system controls, fine-grained UI) |
| Wake word | **Picovoice Porcupine** | On-device, low-power, custom wake word ("Hey Moonwalk"); no audio leaves the Mac for detection |
| Voice output | **Google Cloud TTS Neural2** | Natural prosody; concurrent sentence synthesis for streaming feel; OGG/Opus output plays natively in Chromium |
| Browser bridge | **Chrome Extension MV3** | Bridges the real browser the user has open (with all their logged-in state and cookies) rather than a headless browser |
| Cloud | **Google Cloud Run** | Scales to 0 (no idle cost); 15-minute request timeout for long tasks; WebSocket support |
| Persistence | **Firestore + GCS** | Serverless; vector search built-in; automatic scaling; same GCP credentials |
| Semantic search | **Gemini text-embedding-004** | 768 dimensions; high accuracy; same API key as the LLM |
| Distribution | **electron-builder + GCS** | Universal binary (ARM + Intel); notarization hook; public GCS bucket for download links |

### Key Design Decisions

**Why SPAV instead of a simple chat loop?**
Early versions used a naive "LLM picks a tool, executes, repeats" loop. This failed on multi-step tasks because there was no separation between planning and execution. SPAV adds an explicit planning stage that produces observable, verifiable milestones — making complex tasks far more reliable and debuggable.

**Why milestone-based planning instead of step-by-step scripts?**
A step plan tells the LLM exactly which tool calls to make. A milestone plan tells it what observable outcome to achieve. Milestones are more robust because:
- The executor can adapt if an early step fails (different path, same goal)
- The verifier confirms the milestone was truly achieved before moving on
- Independent milestones can run in parallel (via `SubAgentManager`)
- The LLM isn't over-constrained — it has full autonomy within each milestone scope

**Why a separate Chrome extension instead of Electron's browser?**
Moonwalk is a desktop overlay, not a browser. The Chrome extension bridges the real browser the user already has open, complete with their logged-in sessions, cookies, and browser history. This means web automation works on any site, including banking and internal tools.

**Why split local + cloud modes?**
macOS-specific APIs (AppleScript, Quartz Accessibility, Picovoice) can only run on a Mac. The AI can run anywhere. The split lets the heavy AI scale independently on Cloud Run (and cost $0 when idle) while keeping all macOS operations local and private. Each user's Mac is its own secure execution environment.

**Memory architecture — four tiers**
Moonwalk mirrors how humans actually remember things:

| Tier | Implementation | Scope | Persistence |
|------|---------------|-------|-------------|
| Working memory | In-process Python dict | Current task, action log, entities | Until task ends |
| Short-term | `ConversationMemory` (20 turns) | Recent conversation | 30 min idle timeout |
| Long-term | `UserProfile` (auto-extracted facts) | Who you are, what you like | Permanent |
| Vault | `VaultMemory` (explicit storage) | Things you told Moonwalk to remember | Permanent, semantic search |

**How the Chrome extension element referencing works**
The content script assigns a stable `ref` ID to every interactive element on every page. These IDs are based on element position, tag, role, and text content — they survive re-renders if the element itself doesn't change. The agent uses these refs to click or type into elements reliably (e.g. `browser_click_ref(ref="btn_submit_1")`) rather than fragile CSS selectors or XPath.

---

## 14. Building a DMG for Distribution

### App Icon

electron-builder requires `build/icon.icns`. Create it from a 1024×1024 PNG:

```bash
mkdir -p build/icon.iconset
for size in 16 32 64 128 256 512; do
  sips -z $size $size icon.png --out build/icon.iconset/icon_${size}x${size}.png
  sips -z $((size*2)) $((size*2)) icon.png \
    --out build/icon.iconset/icon_${size}x${size}@2x.png
done
iconutil -c icns build/icon.iconset -o build/icon.icns
rm -rf build/icon.iconset
```

Then add to `package.json` → `build.mac`:
```json
"icon": "build/icon.icns"
```

### Build

```bash
# Universal DMG (Apple Silicon + Intel in one file)
npm run build:signed

# Output: dist/Moonwalk-1.0.0-universal.dmg
```

The DMG bundles: Electron runtime, all renderer files, the `backend/` folder, `hey_moonwalk.ppn`, `chrome_extension/`, and `setup.sh` (runs automatically on first launch to create the Python venv).

---

## 15. Code Signing & Notarization

Notarization removes the Gatekeeper security warning so customers can double-click to open the app without any friction.

### Prerequisites

- **Apple Developer Program** membership ($99/year) — [developer.apple.com](https://developer.apple.com)
- **Developer ID Application** certificate installed in your macOS Keychain (download from the Apple Developer portal)
- An **app-specific password** — generate at [appleid.apple.com](https://appleid.apple.com/account/manage) under *Sign-In and Security → App-Specific Passwords*
- Your 10-character **Team ID** — find it at [developer.apple.com/account](https://developer.apple.com/account) → Membership Details

### Set Environment Variables

```bash
# Identity installed in Keychain (exact string from Keychain Access)
export CSC_NAME="Developer ID Application: Your Name (XXXXXXXXXX)"

# Apple notary credentials
export APPLE_ID="your@apple.id"
export APPLE_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"   # App-specific, NOT your Apple ID password
export APPLE_TEAM_ID="XXXXXXXXXX"                  # 10-character Team ID
```

### Build with Full Signing + Notarization

```bash
npm run build:signed
```

The `build/notarize.cjs` afterSign hook runs automatically after signing. It submits the `.app` to Apple's notary service and waits for approval (typically 2–5 minutes). Once approved, any Mac in the world can open the app by double-clicking — no Gatekeeper warning.

### Without Signing (development only)

Leave the env vars unset. The build still produces a working unsigned DMG. Customers must right-click → Open on first launch.

---

## 16. Distributing to Customers via GCS

### One-Command Release

```bash
export GCP_PROJECT="your-project-id"   # Your GCP project
./scripts/release.sh
```

This pipeline:
1. **Packages the Chrome extension** → `dist/moonwalk-browser-bridge.zip` (with an `INSTALL.md` guide inside for customers)
2. **Builds the signed + notarized DMG** → `dist/Moonwalk-1.0.0-universal.dmg`
3. **Uploads to GCS** → creates versioned files, `latest/` aliases, and a download page

### Step-by-Step (if you need more control)

```bash
# 1. Package extension
npm run dist:extension
# → dist/moonwalk-browser-bridge.zip (49 KB)

# 2. Build DMG
npm run build:signed
# → dist/Moonwalk-1.0.0-universal.dmg

# 3. Upload to GCS
npm run dist:upload
# → Prints all public URLs
```

### What Gets Uploaded

| File | Public URL |
|------|-----------|
| `Moonwalk-1.0.0-universal.dmg` | `.../releases/v1.0.0/Moonwalk-1.0.0-universal.dmg` |
| Latest DMG alias | `.../releases/latest/Moonwalk-latest.dmg` |
| Extension zip | `.../releases/latest/moonwalk-browser-bridge.zip` |
| Download page | `.../releases/index.html` |

### Share with Customers

Send customers **one URL**:
```
https://storage.googleapis.com/<project>-moonwalk-releases/releases/index.html
```

It's a clean, branded page with big download buttons and brief install instructions — no technical knowledge required.

---

## 17. Cloud Deployment (GCP Cloud Run)

### One-Command Deploy

```bash
# Requires: GEMINI_API_KEY set, gcloud CLI authenticated
GCP_PROJECT="your-project-id" \
GEMINI_API_KEY="your-gemini-key" \
bash deploy/deploy_gcp.sh
```

This script handles everything:
1. Enables required APIs (Cloud Run, Firestore, Storage, Artifact Registry, Cloud Build)
2. Creates Firestore database with vector index for RAG
3. Creates GCS memory bucket with 90-day lifecycle policy
4. Creates Artifact Registry Docker repository
5. Builds Docker image with Cloud Build (no local Docker needed)
6. Deploys `moonwalk-brain` service to Cloud Run
7. Prints the WebSocket URL + instructions for connecting Mac clients

### After Deployment

Health check:
```bash
curl https://moonwalk-brain-xxxx.us-central1.run.app/health
# → {"status":"ok","agents":0}
```

Configure a customer's Mac to use the cloud brain (`backend/.env`):
```bash
MOONWALK_CLOUD_URL=wss://moonwalk-brain-xxxx.us-central1.run.app
MOONWALK_CLOUD_TOKEN=your-shared-secret
```

They then launch `mac_client.py` instead of `local_server.py` — audio and macOS tools stay local, AI reasoning goes to Cloud Run.

### Cloud Run Settings

| Setting | Value | Reason |
|---------|-------|--------|
| CPU | 2 | Sufficient for async agent loop |
| Memory | 1 Gi | Handles concurrent WebSocket sessions |
| Min instances | 0 | $0 when nobody is using it |
| Max instances | 3 | Caps cost for multi-user scenarios |
| Timeout | 900s | Allows long multi-step tasks to complete |
| Session affinity | On | WebSocket connections stay on the same instance |

---

## 18. Running Tests

```bash
# Full test suite
cd tests && bash run_test.sh

# Individual test modules
python -m pytest tests/test_agent_v2.py -v              # Core agent loop
python -m pytest tests/test_milestone_executor.py -v    # Milestone execution
python -m pytest tests/test_milestone_planning.py -v    # Plan generation
python -m pytest tests/test_browser_scenarios.py -v     # Browser automation
python -m pytest tests/test_verifier_aci.py -v          # Verification engine
python -m pytest tests/test_multi_agent.py -v           # Parallel sub-agents
python -m pytest tests/test_reliability_recovery.py -v  # Error recovery
python -m pytest tests/test_replanning.py -v            # Dynamic replanning
python -m pytest tests/test_router.py -v                # Model routing

# WebSocket integration test
node tests/ws_test.js

# Quality + intelligence benchmarks
python benchmarks/run_benchmarks.py
```

---

*Last updated: 16 March 2026*
