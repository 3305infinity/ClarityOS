# Moonwalk — Architecture

> **This is the single authoritative architecture document.** It reflects the current production runtime.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Transport Layer](#2-transport-layer)
3. [Agent Pipeline — SPAV Loop](#3-agent-pipeline--spav-loop)
4. [Personality & Small-Talk Fast Path](#4-personality--small-talk-fast-path)
5. [Interrupt System](#5-interrupt-system)
6. [Voice Output — Streamed TTS](#6-voice-output--streamed-tts)
7. [Conversation Mode](#7-conversation-mode)
8. [Browser Automation](#8-browser-automation)
9. [Tools & Providers](#9-tools--providers)
10. [Memory](#10-memory)
11. [UI — Glass Pill](#11-ui--glass-pill)
12. [File Map](#12-file-map)

---

## 1. System Overview

```
┌──────────────────────────────────────────────────────────┐
│                    Electron Shell                        │
│  ┌─────────────────┐    ┌──────────────────────────────┐ │
│  │  Glass Pill UI  │◄──►│  renderer.js (WebSocket WS)  │ │
│  │  (index.html)   │    │  + AudioContext TTS playback  │ │
│  └─────────────────┘    └──────────────┬───────────────┘ │
└─────────────────────────────────────────┼────────────────┘
                                          │ ws://localhost:8000
                    ┌─────────────────────▼───────────────────┐
                    │       local_server.py  (Python WS)       │
                    │  • receive text/audio/actions            │
                    │  • spawn MoonwalkAgentV2.run()           │
                    │  • stream TTS chunks back to renderer    │
                    │  • handle cancel / conversation mode     │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │          MoonwalkAgentV2                 │
                    │  (backend/agent/core_v2.py)              │
                    │  SPAV loop:                              │
                    │  Sense → Route → Plan/Act → Verify       │
                    └─────────────────────────────────────────┘
```

**Key properties:**
- Electron overlay sits always-on-top; activated by wake word (`Hey Moonwalk`) or Escape/click
- Single WebSocket connection per session on `localhost:8000`
- Python backend is spawned as a child process by `main.js` on app start
- All agent work runs in the Python process; Electron is a thin display shell

---

## 2. Transport Layer

**File:** `backend/servers/local_server.py`

### WebSocket message types

| Direction | `type` | Payload | Purpose |
|-----------|--------|---------|---------|
| Client → Server | `text_input` | `{text}` | User typed or voice-transcribed text |
| Client → Server | `audio_chunk` | `{audio}` (base64 WAV) | Raw mic chunk for STT |
| Client → Server | `user_action` | `{action, data}` | UI approval / rejection |
| Client → Server | `cancel_task` | — | Interrupt current task |
| Client → Server | `toggle_conversation_mode` | — | Toggle persistent-listen mode |
| Client → Server | `tts_done` | — | Renderer finished playing the audio queue |
| Server → Client | `ack` | `{text}` | Instant acknowledgement before heavy work |
| Server → Client | `response` | `{text, awaiting_input}` | Final agent response |
| Server → Client | `tts_chunk` | `{audio}` (base64 OGG) | One synthesised audio sentence |
| Server → Client | `tts_stop` | — | Immediately stop TTS playback |
| Server → Client | `tts_done` | — | All TTS chunks for this turn have been sent |
| Server → Client | `conversation_mode` | `{enabled}` | Notify renderer of mode change |
| Server → Client | `state` | `{state, text}` | UI state update (loading / doing / idle) |
| Server → Client | `error` | `{message}` | Error surfaced to user |

### Request lifecycle

```
1. audio_chunk arrives  → STT via Whisper / Deepgram
2. text_input arrives   → send ack immediately
3. agent.run() spawned  → streams state updates
4. response produced    → _stream_tts() synthesises and streams tts_chunks
5. tts_done sent        → if conversation_mode: auto-start mic listen
```

---

## 3. Agent Pipeline — SPAV Loop

**File:** `backend/agent/core_v2.py` — `MoonwalkAgentV2`

The core loop follows **Sense → Route → Plan → Act → Verify**:

```
User text
    │
    ▼
① SENSE
   WorldState assembly
   • screen perception (screenshots, accessibility tree)
   • open app / active URL context
   • running agent list
    │
    ▼
② ROUTE
   _is_conversational()  ──yes──► Fast Path (< 500 ms)
                         │        (greetings, thanks, small-talk, factual Q)
                         no
                         │
                         ▼
   Full pipeline continues
    │
    ▼
③ PLAN  (task_planner.py)
   Build MilestonePlan
   • compound-task decomposition
   • skill-overlay hints from template_registry
   • sync fallback for simple single-step tasks
    │
    ▼
④ ACT  (milestone_executor.py)
   LLM micro-loop per milestone:
   • select tools (selector.py)
   • execute tool call
   • observe result
   • advance or retry milestone
   • cooperative cancellation check each iteration
    │
    ▼
⑤ VERIFY  (verifier.py)
   Evidence gate:
   • per-tool verification strategies
   • pass → update working memory, advance
   • fail → retry or surface error
    │
    ▼
   Final response  →  ws_callback(response)
```

### Instant acknowledgement

Before the pipeline starts, `_pick_ack()` picks a brief phrase ("On it", "Sure", "Let me check", etc.) and sends `{type: "ack", text: "…"}` down the WebSocket. The renderer briefly shows this text while the state transitions to LOADING.

### WorldState

`backend/agent/world_state.py` — assembled before every pipeline run:
- `UserIntent` (action + target + parameters parsed from text)
- `TaskGraph` (dependency model for compound tasks)
- Screen snapshot + active app metadata from perception layer

### MilestonePlan

`backend/agent/task_planner.py` + `backend/agent/planner.py`

- The **only** active planning unit in the V2 runtime
- Each milestone is a concrete, verifiable sub-goal
- Template overlays from `template_registry.py` are **advisory only** — the LLM milestone executor owns final decisions

---

## 4. Personality & Small-Talk Fast Path

**File:** `backend/agent/core_v2.py`

### System prompt personality section

`SYSTEM_PROMPT_V2` includes a `## Personality & Tone` block that instructs the LLM to:
- be warm, direct, and concise (macOS assistant register)
- skip preamble for action tasks; lead with the action
- use first-person natural language for conversational turns
- avoid corporate filler phrases
- signal conversation mode intent via `[CONVERSATION_MODE_ON]` / `[CONVERSATION_MODE_OFF]` markers

### Small-talk fast path

```python
_is_conversational(text) → bool
```

Regex classifier checks for: greetings, thanks, farewells, mood queries, simple factual questions. If matched:

```python
_try_conversational_fast_path(text, ws_callback) → response | None
```

Sends the request directly to a Flash LLM (no planning, no tools, no perception). Typical latency: **< 500 ms**. If the Flash call fails or returns empty, falls through to the full pipeline.

### Proactive follow-up

In conversation mode (35% chance), after producing a response `_suggest_followup()` appends a contextual follow-up question to keep the dialogue alive.

### Conversation mode marker

If the agent decides the session warrants persistent listening (e.g., user opens a multi-turn conversation), it embeds `[CONVERSATION_MODE_ON]` in its response text. `local_server.py` strips the marker and activates conversation mode automatically — no UI button required.

---

## 5. Interrupt System

**Files:** `backend/servers/local_server.py`, `backend/runtime_state.py`, `renderer/renderer.js`

### Cancellation signals

| Trigger | Path |
|---------|------|
| **Stop button** (glass pill) | `cancelActiveTask()` → WS `cancel_task` → `cancel_active_task()` → `runtime_state_store.cancel_request()` |
| **Escape key** | Same as stop button |
| **Voice "stop" / "cancel"** | STT text matches → same backend cancel path |
| **TTS stop only** | `stopTTS()` client-side (no backend call needed) |

### Cooperative cancellation

`runtime_state_store.cancel_request()` sets a shared flag. `MilestoneExecutor` checks this flag at the top of each micro-loop iteration and raises `CancelledError` when set. This prevents blocking the event loop while still aborting mid-plan.

### TTS and mic muting

While TTS is playing, the mic pipeline is muted (`tts_playing` flag). This prevents the assistant from hearing its own voice and triggering a spurious new request.

---

## 6. Voice Output — Streamed TTS

**File:** `backend/voice/tts.py`

### Architecture

```
agent response text
      │
      ▼
 prepare_for_speech()  — strip markdown, code blocks, URLs
      │
      ▼
 split_sentences()  — chunk at sentence boundaries (max 4 800 chars/call)
      │
      ▼
 asyncio.gather (max 4 concurrent)
      │  for each sentence:
      ▼
 Google Cloud TTS Neural2
 • voice: en-US-Neural2-J (default)
 • encoding: OGG_OPUS
 • speaking rate: 1.05
      │
      ▼
 base64-encoded OGG chunks
 sent as {type: "tts_chunk", audio: "..."} WS messages
      │
      ▼
 {type: "tts_done"} sentinel
```

### Renderer playback (`renderer.js`)

```
tts_chunk arrives → handleTTSChunk() → push to ttsQueue
                                          │
                               if not playing → playNextTTSChunk()
                                          │
                               AudioContext.decodeAudioData(OGG)
                               source.start() → onended → playNextTTSChunk()
```

Sequential playback: next chunk starts immediately when current one ends. `stopTTS()` disconnects the active source and clears the queue.

### TTSEngine singleton

`get_tts_engine()` returns a process-wide `TTSEngine` instance, lazily initialised on first TTS request. Google Cloud credentials are read from the environment (`GOOGLE_APPLICATION_CREDENTIALS`).

---

## 7. Conversation Mode

**Files:** `backend/servers/local_server.py`, `backend/agent/core_v2.py`

### Activation

- **Agent-activated only.** The agent embeds `[CONVERSATION_MODE_ON]` in its response when it determines the user is in a multi-turn conversational flow.
- No UI button. No manual toggle from the user.

### Behaviour while active

1. After TTS finishes (or immediately if TTS is disabled), the mic is automatically re-activated — no wake word needed.
2. A 120-second inactivity timer runs. If no new input arrives within 120 s, conversation mode deactivates automatically.
3. Each user turn resets the timer.
4. The agent can deactivate it by embedding `[CONVERSATION_MODE_OFF]`.

### State tracking

```python
# local_server.py (AssistantSession)
self._conversation_mode: bool
self._conversation_timer: asyncio.TimerHandle | None

toggle_conversation_mode() → bool   # returns new state
_reset_conversation_timer()          # restart 120 s timer
_cancel_conversation_timer()         # cancel on manual deactivation
_conversation_timeout()              # called after 120 s idle
```

### Renderer notification

Server sends `{type: "conversation_mode", enabled: true/false}`. The renderer auto-starts mic capture when `enabled` is true and TTS is not playing.

### UI

No visual indicator beyond the standard blue listening glow (existing `variant-listening` state). No LIVE badge, no green border.

---

## 8. Browser Automation

**Files:** `backend/browser/`, `chrome_extension/`

- Chrome extension (`content_script.js`) injects into pages to provide DOM snapshots and element interaction
- `backend/browser/browser_aci.py` — high-level ACI (Action-Click-Input) tools used by the milestone executor
- `backend/browser/browser_tools.py` — raw browser-ref tool wrappers
- `backend/agent/browser_intent_utils.py` — browser-specific reasoning helpers
- Perception layer captures screenshots + accessibility tree; passed into WorldState before each pipeline run

---

## 9. Tools & Providers

**Files:** `backend/tools/`, `backend/providers/`

### Tool selection

`backend/tools/selector.py` — narrows the full tool surface to the request-relevant subset before milestone execution. Reduces token overhead and prevents the LLM from attempting irrelevant tool calls.

### Providers

`backend/providers/` — abstraction layer over LLM providers (OpenAI, Google Gemini, Anthropic). The milestone executor and fast path call into provider adapters; no provider-specific code leaks into agent logic.

---

## 10. Memory

**Files:** `backend/agent/memory.py`, `backend/agent/cloud_memory.py`, `backend/agent/rag.py`

- **Working memory** — per-request context built during milestone execution (tool results, observations, intermediate facts)
- **Cloud memory** (`cloud_memory.py`) — cross-session persistent memory stored in GCS / Firestore
- **RAG** (`rag.py`) — retrieval-augmented generation over stored memory and user documents
- Memory is updated by the verifier after successful milestone completion

---

## 11. UI — Glass Pill

**Files:** `renderer/index.html`, `renderer/styles.css`, `renderer/renderer.js`

### States

| State | Visual | When |
|-------|--------|------|
| `idle` | Small pill, no glow | No activity |
| `listening` | Blue glow pulse | Mic active, waiting for speech |
| `loading` | Spinner | Agent processing, ack text briefly shown |
| `doing` | Doing text + stop button | Milestone execution in progress |
| `response` | Expanded text card | Agent response displayed |
| `speaking` | Blue glow pulse | TTS playback active |

### Stop button

`#pill-stop` — visible during `loading` and `doing` states. Clicking it calls `cancelActiveTask()`, which sends `cancel_task` to the backend and also calls `stopTTS()` locally.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Escape` | Cancel active task (or stop TTS if task already done) |
| Wake word | Activate mic from idle |

---

## 12. File Map

```
backend/
  servers/
    local_server.py         WebSocket entrypoint, session management,
                            TTS streaming, cancel, conversation mode
  agent/
    core_v2.py              MoonwalkAgentV2 — SPAV loop, ack, fast path,
                            personality, conversation mode markers
    task_planner.py         MilestonePlan builder
    planner.py              Milestone + ExecutionStep dataclasses
    milestone_executor.py   LLM micro-loop, cooperative cancellation
    verifier.py             Evidence gate / per-tool verification
    world_state.py          WorldState, UserIntent, IntentParser
    memory.py               Working memory
    cloud_memory.py         Cross-session cloud memory
    rag.py                  Retrieval-augmented generation
    browser_intent_utils.py Browser reasoning helpers
    template_registry.py    Advisory skill overlays (JSON packs)
    perception.py           Screen capture + accessibility tree
    glance.py               Fast screen-understanding pass
  voice/
    tts.py                  TTSEngine — Google Cloud Neural2 streamed TTS,
                            split_sentences, prepare_for_speech
    __init__.py
  browser/
    browser_aci.py          High-level ACI browser tools
    browser_tools.py        Raw browser-ref tools
  tools/
    selector.py             Request-scoped tool surface narrowing
  providers/
    (LLM provider adapters)
  runtime_state.py          Shared cancel flag (RuntimeStateStore)

renderer/
  index.html                Glass pill HTML — stop button, response card
  renderer.js               WS client, TTS AudioContext queue, state machine,
                            stop button, Escape key, conversation mode
  styles.css                Pill animations, stop button, state variants

chrome_extension/
  content_script.js         DOM snapshot + element interaction injector
  background.js             Extension background worker

main.js                     Electron main — spawns Python backend, creates
                            always-on-top overlay window
```
