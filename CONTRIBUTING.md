# Moonwalk — Developer Sync Guide

> **Last updated:** 2026-03-03
> **Purpose:** This document is the single source of truth for anyone working on this project. If you are using AI tools to build, paste this entire file into context.

---

## 1. Project Overview

Moonwalk is a **voice-activated AI assistant** that runs as a macOS overlay (Electron) with a Python backend powered by Google's Gemini LLM. The LLM can control the user's Mac (open apps, click UI, type text) and also spawn autonomous background "sub-agents" that run headlessly on the cloud.

### Architecture Diagram

```
┌──────────────────────────────────────────────┐
│           Electron (main.js)                 │
│  ┌──────────────┐   ┌───────────────────┐    │
│  │  Overlay UI  │   │  Agent Dashboard  │    │
│  │ renderer.js  │   │  dashboard.js     │    │
│  │ index.html   │   │  dashboard.html   │    │
│  │ styles.css   │   │  dashboard.css    │    │
│  └──────┬───────┘   └────────┬──────────┘    │
│         │  WebSocket (ws://127.0.0.1:8000)   │
│         └──────────────┬─────┘               │
└────────────────────────┼─────────────────────┘
                         │
      ┌──────────────────▼──────────────────┐
      │     Python Backend (backend/)       │
      │                                     │
      │  backend_server.py  (local entry)   │
      │  cloud_server.py    (cloud entry)   │
      │  agent.py           (LLM loop)      │
      │  model_router.py    (tier routing)  │
      │  providers.py       (Gemini API)    │
      │  memory.py          (conversation)  │
      │  perception.py      (Mac context)   │
      │  tools.py           (33+ tools)     │
      └────────────────────────────────────┘
```

---

## 2. Folder Structure

```
Moonwalk/
├── main.js                 # Electron main process (window mgmt, IPC, hotkeys)
├── preload.js              # Electron context bridge (IPC API for renderer)
├── package.json            # Electron dependencies (electron, ws)
├── Dockerfile              # Cloud Run deployment (backend only)
│
├── renderer/               # ── FRONTEND (Electron Renderer) ──
│   ├── index.html          #   Main overlay UI (pill, bubble, drawer)
│   ├── styles.css          #   Overlay styles
│   ├── renderer.js         #   Overlay logic (WS, audio, UI states)
│   ├── dashboard.html      #   Agent Dashboard UI
│   ├── dashboard.css       #   Dashboard styles
│   └── dashboard.js        #   Dashboard logic (WS, agent cards, HITL)
│
├── backend/                # ── BACKEND (Python) ──
│   ├── .env                #   API keys & model config
│   ├── backend_server.py   #   Local entry point (voice + WS server)
│   ├── cloud_server.py     #   Cloud entry point (GCP Cloud Run)
│   ├── agent.py            #   Core LLM agent loop
│   ├── model_router.py     #   Tier routing (fast vs powerful model)
│   ├── providers.py        #   Gemini API wrapper (streaming)
│   ├── memory.py           #   Conversation memory + context compression
│   ├── perception.py       #   macOS context snapshot (active app, URL)
│   ├── tools.py            #   All 33+ tool definitions
│   ├── hey_moonwalk.ppn    #   Wake word model (Picovoice)
│   └── requirements-cloud.txt  # Python deps for GCP deployment
│
├── tests/                  # Test scripts (not production)
└── experiments/            # Generated prototypes (CRM, etc.)
```

---

## 3. File Ownership Rules

| Area | Owner | Files |
|------|-------|-------|
| **Overlay UI** | Frontend Dev | `renderer/index.html`, `renderer/styles.css`, `renderer/renderer.js` |
| **Dashboard UI** | Frontend Dev | `renderer/dashboard.html`, `renderer/dashboard.css`, `renderer/dashboard.js` |
| **Electron Shell** | Frontend Dev | `main.js`, `preload.js`, `package.json` |
| **LLM Brain** | Backend Dev | `backend/agent.py`, `backend/providers.py`, `backend/model_router.py`, `backend/memory.py` |
| **Tool Registry** | Backend Dev | `backend/tools.py` |
| **Server & Orchestrator** | Backend Dev | `backend/backend_server.py`, `backend/cloud_server.py` |
| **Perception** | Backend Dev | `backend/perception.py` |
| **Shared Contract** | Both | This file (`CONTRIBUTING.md`) — the WebSocket protocol below |

> **Rule:** Never edit another owner's files without a Pull Request discussion first.

---

## 4. The WebSocket Protocol (API Contract)

The frontend and backend communicate exclusively via a single **WebSocket** connection on `ws://127.0.0.1:8000` (local) or `ws://CLOUD_IP:8080` (cloud).

All messages are JSON. Every message has a `"type"` field.

### 4a. Frontend → Backend (Messages the frontend SENDS)

| `type` | Purpose | Payload |
|--------|---------|---------|
| `audio_chunk` | Raw audio from the microphone (base64 WAV) | `{ "payload": "<base64>" }` |
| `hotkey_pressed` | User pressed the global hotkey (⌘⇧Space) | `{}` |
| `text_input` | User typed text directly (not voice) | `{ "text": "...", "context": { "active_app": "...", "window_title": "...", "browser_url": "..." } }` |
| `dashboard_action` | User clicked a button on the dashboard | `{ "action": "stop_agent" \| "pause_agent", "agent_id": "abc123" }` |
| `dashboard_sync` | Dashboard requests full state snapshot | `{}` |
| `resume_agent` | User approves or provides feedback for a paused agent | `{ "agent_id": "abc123", "action": "approve" \| "feedback", "feedback": "..." }` |

### 4b. Backend → Frontend (Messages the frontend RECEIVES)

| `type` | Purpose | Payload |
|--------|---------|---------|
| `status` | Change the overlay UI state | `{ "state": "state-idle" \| "state-listening" \| "state-loading" }` |
| `thinking` | Agent is reasoning (show loading dots) | `{}` |
| `doing` | Agent is executing a tool | `{ "text": "Opening Safari...", "app": "Safari", "icon_url": "..." }` |
| `response` | Agent's final answer | `{ "payload": { "text": "Done!", "app": "Safari", "display": "card" \| "pill", "await_input": false } }` |
| `await_reply` | Agent is waiting for user's voice reply | `{}` |
| `sub_agent_update` | Background agent status changed | See §5 below |
| `dashboard_state` | Full snapshot of all agents (response to `dashboard_sync`) | `{ "agents": { "<id>": { ...state } } }` |
| `ipc_trigger` | Backend wants Electron to do something | `{ "command": "open-dashboard" }` |
| `tool_request` | (Cloud mode) Backend asks Mac Client to run a macOS tool | `{ "call_id": "abc", "tool_name": "open_app", "tool_args": { "app_name": "Safari" } }` |
| `pong` | Response to a `ping` | `{}` |

### 4c. `sub_agent_update` Detail

The `sub_agent_update` message is how the backend streams agent lifecycle events to the UI.

```json
{
  "type": "sub_agent_update",
  "agent_id": "a1b2c3d4",
  "status": "<status>",
  // + optional fields depending on status
}
```

| `status` value | Meaning | Extra fields |
|----------------|---------|-------------|
| `spawned` | A new agent was just created | `task` |
| `running` | Agent is actively working | — |
| `progress` | Agent logged a progress message | `message` |
| `checklist_updated` | Agent generated/updated its task checklist | `checklist: string[]` |
| `paused_for_review` | Agent paused and needs human approval | `review_topic`, `result` |
| `completed` | Agent finished successfully | `result`, `task` |
| `error` | Agent crashed | `error` |
| `stopped` | Agent was manually stopped | — |
| `log` | Agent appended a log entry | `message` |

---

## 5. Agent State Machine

Every background agent follows this state lifecycle:

```
  ┌─────────┐
  │ SPAWNED │
  └────┬────┘
       ▼
  ┌─────────┐     request_human_review()     ┌────────────────────┐
  │ RUNNING │ ─────────────────────────────▶ │ PAUSED_FOR_REVIEW  │
  └────┬────┘                                └─────────┬──────────┘
       │                                               │
       │  sub_agent_complete()                         │ resume_agent (UI)
       ▼                                               ▼
  ┌───────────┐                                   Back to RUNNING
  │ COMPLETED │
  └───────────┘
       │  (on error)        (user clicks Stop)
       ▼                          ▼
  ┌────────┐               ┌──────────┐
  │ FAILED │               │ STOPPED  │
  └────────┘               └──────────┘
```

**Backend enum values** (in `cloud_server.py`):
- `running`, `waiting_on_child`, `paused_for_review`, `completed`, `error`, `stopped`, `stopping`

---

## 6. Agent State Object Schema

When the dashboard receives a full sync (`dashboard_state`), each agent object has this shape:

```jsonc
{
  "task": "Build a CRM with React",           // string — task description
  "status": "running",                        // string — see state machine above
  "intrusive": false,                         // bool — has Mac UI access?
  "system_prompt": null,                      // string|null — custom persona
  "allowed_tools": null,                      // string[]|null — tool whitelist
  "deliverable_format": null,                 // string|null — output format
  "created_at": 1709481234.56,                // float — unix timestamp
  "completed_at": null,                       // float|null
  "logs": ["[14:30:01] Scaffolding project"], // string[] — progress logs
  "checklist": ["Set up Next.js", "..."],     // string[] — planned steps
  "result": null,                             // string|null — final output
  "error": null,                              // string|null — error message
  "iterations": 5                             // int — LLM loop count (max 25)
}
```

---

## 7. Tool Registry (All 33+ Tools)

Tools are Python async functions registered in `backend/tools.py`. The LLM picks which tool to call.

### Cloud-Safe Tools (run on GCP, no Mac needed)
| Tool | Description |
|------|-------------|
| `send_response` | Send final answer text to the user |
| `await_reply` | Ask user a question, wait for voice reply |
| `fetch_web_content` | HTTP GET a URL, return text |
| `run_python` | Execute Python code |
| `run_shell` | Execute a shell command |
| `read_file` / `write_file` / `replace_in_file` | File I/O |
| `list_directory` | List folder contents |
| `think` | Internal reasoning scratchpad |

### macOS Tools (require `intrusive=True`)
| Tool | Description |
|------|-------------|
| `open_app` | Launch a macOS app |
| `close_window` / `quit_app` | Close windows/apps |
| `type_text` | Type text via keyboard |
| `run_shortcut` | Press keyboard shortcuts |
| `click_element` | Click a screen element |
| `read_screen` | OCR the current screen |
| `play_media` | Play music/video |
| `set_volume` | Set system volume |

### Agent Management Tools
| Tool | Description |
|------|-------------|
| `spawn_agent` | Create a new background agent |
| `list_agents` | List all agents and statuses |
| `get_agent_output` | Read an agent's logs/result |
| `stop_agent` | Stop a running agent |
| `delegate_to_subagent` | Manager spawns a worker |
| `wait_for_agent` | Block until an agent finishes |
| `message_agent` | Send a message to another agent |

### Agent-Internal Tools (only used BY running agents)
| Tool | Description |
|------|-------------|
| `sub_agent_log` | Log a progress message |
| `sub_agent_complete` | Mark task as done |
| `generate_checklist` | Create a task checklist |
| `request_human_review` | Pause for human approval |
| `check_messages` | Read messages from other agents |

---

## 8. Environment Variables

Located in `backend/.env`. Defaults come from `backend/providers/models.py`, which is
the only place model ids are defined — do not re-spell them with inline
`os.environ.get("GEMINI_FAST_MODEL", "…")` calls:

```env
GEMINI_API_KEY=<your-key>              # Required. Get from aistudio.google.com
GEMINI_FAST_MODEL=gemini-3.5-flash      # Tier 1 (simple tasks)
GEMINI_POWERFUL_MODEL=gemini-3.5-flash  # Tier 2 (complex tasks)
GEMINI_ROUTING_MODEL=gemini-3.5-flash   # Tier 0 (query classifier)
```

---

## 9. How to Run Locally

### Frontend (Electron)
```bash
cd Moonwalk
npm install
npm start          # Starts Electron + auto-launches Python backend
```

### Backend Only (for isolated backend dev)
```bash
cd Moonwalk
source venv/bin/activate
python backend/backend_server.py     # Starts WS server on :8000
```

### Cloud Deployment (GCP)
```bash
docker build -t moonwalk-brain .
docker run -p 8080:8080 -e GEMINI_API_KEY=<key> moonwalk-brain
```

> **Note:** The `Dockerfile` currently copies files from the root. It needs updating to copy from `backend/` after the recent folder restructure.

---

## 10. Git Branching Strategy

```
main                      ← stable, production-ready
├── feat/frontend-*       ← frontend work (renderer, dashboard)
├── feat/backend-*        ← backend work (agent, tools, providers)
└── fix/*                 ← bug fixes
```

**Rules:**
1. Never commit directly to `main`.
2. Create feature branches: `git checkout -b feat/backend-new-tool`
3. Open a Pull Request to merge into `main`.
4. The other developer reviews before merging.

---

## 11. Adding a New Tool (Backend Dev)

1. Open `backend/tools.py`
2. Add your tool using the `@registry.register()` decorator:
```python
@registry.register(
    name="my_new_tool",
    description="What this tool does",
    parameters={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "..."}
        },
        "required": ["param1"]
    }
)
async def my_new_tool(param1: str = "") -> str:
    # Your implementation
    return "Result string"
```
3. If the tool is cloud-safe, add it to `CLOUD_TOOLS` in `cloud_server.py`.
4. The frontend does NOT need to change — tool results flow through the existing WebSocket protocol.

---

## 12. Adding a New UI Component (Frontend Dev)

1. The overlay UI lives in `renderer/index.html` + `renderer/renderer.js` + `renderer/styles.css`.
2. The dashboard lives in `renderer/dashboard.html` + `renderer/dashboard.js` + `renderer/dashboard.css`.
3. All data comes from WebSocket messages — refer to §4b for the message types.
4. To send a command to the backend, use `app.ws.send(JSON.stringify({ type: "...", ... }))`.
5. To trigger Electron native actions, use the `bridge` API from `preload.js`:
   - `bridge.hideWindow()`
   - `bridge.enableMouse()` / `bridge.disableMouse()`
   - `bridge.openDashboard(agentId)`
   - `bridge.logError(msg)` / `bridge.logInfo(msg)`

---

## 13. Updating the Dockerfile

After the folder restructure, the `Dockerfile` needs to be updated. The `COPY` commands should reference `backend/`:

```dockerfile
COPY backend/requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY backend/cloud_server.py .
COPY backend/agent.py .
COPY backend/providers.py .
COPY backend/model_router.py .
COPY backend/memory.py .
COPY backend/tools.py .
COPY backend/.env .
```
