# ClarityOS — Gnani AI integration

**Great Indian AI Internship Challenge 2026**

ClarityOS is a Windows desktop assistant that listens, acts on real
applications, and talks back. **Every piece of speech in it runs on Gnani.**
There is no second speech vendor in the codebase and no setting that switches
to one — `GNANI_API_KEY` is what makes the microphone and the speaker work.

This document maps each Gnani model to the exact file and function that calls
it, and says plainly which parts of the product are *not* Gnani.

---

## What runs on Gnani

| Model | Transport | Endpoint | File | Function |
|---|---|---|---|---|
| **Prisma v2.5** (STT) | WebSocket | `wss://api.vachana.ai/stt/v3/stream` | `backend/voice/stt.py` | `GnaniStreamSession.open / send_pcm / commit / finish` |
| **Prisma v2.5** (STT) | REST | `POST https://api.vachana.ai/stt/v3` | `backend/voice/stt.py` | `GnaniSTT.transcribe` |
| **Timbre v2.5** (TTS) | SSE | `POST https://api.vachana.ai/api/v1/tts/sse` | `backend/voice/tts_providers.py` | `GnaniTTS.synthesize_stream` |
| **Timbre v2.5** (TTS) | REST | `POST https://api.vachana.ai/api/v1/tts/inference` | `backend/voice/tts_providers.py` | `GnaniTTS.synthesize` |

Both models authenticate with `X-API-Key-ID` (REST) / `x-api-key-id` (socket).
Model ids are pinned — `prisma-v2.5` and `timbre-v2.5` — rather than inherited
from a server-side default that could move.

### Speech-to-text — Prisma v2.5

The realtime socket is the default (`CLARITYOS_STT_PROVIDER=gnani-stream`).
Audio is sent **while the user is still speaking**, so the transcript is ready
almost as soon as they stop.

- Frames are raw PCM, signed 16-bit little-endian, mono, **exactly 1024 bytes
  (512 samples)** per frame, as the protocol requires. The splitter is
  `pcmFrames()` in `renderer/renderer.js` and `iter_frames()` in
  `backend/voice/stt.py`; a partial tail is carried to the next frame rather
  than zero-padded.
- The server's own voice-activity detection decides where utterances end.
  `{"type": "commit"}` is sent only when the user has explicitly finished.
  The client's silence detector is switched off while streaming, so the two
  never race.
- Events handled: `connected`, `speech_start`, `processing`, `transcript`,
  `error` — in `GnaniStreamSession._read_events`.
- If the socket cannot be used, `GnaniStreamingSTT.transcribe` falls back to
  the REST endpoint. **That fallback is still Gnani.**
- Clips sent to REST are capped at 30s, the documented maximum, in
  `GnaniSTT.transcribe` and in `AIAssistant._max_clip_seconds`
  (`backend/servers/local_server.py`).
- Word boosting (`bias_list` / `bias_score`) is supported; `bias_score` is
  validated against the documented values and dropped if invalid, so a bad
  setting costs the boost rather than the transcript.

### Text-to-speech — Timbre v2.5

Answers are split into sentences by `split_sentences()` and streamed over SSE,
so speech starts before a sentence has finished rendering.

- `GnaniTTS.synthesize_stream` parses the documented `start` / `chunk` /
  `complete` / `error` events and base64-decodes `chunk.audio`.
- `TTSEngine._stream_from_provider` (`backend/voice/tts.py`) maps each chunk's
  `chunk_index` onto the renderer's running `seq` and `is_final` onto `final`.
- Gnani documents **one active synthesis request at a time**, so REST and SSE
  share a single semaphore (`gnani_gate()`), with a bounded retry on HTTP 429.
- `speed` is clamped to Timbre's documented 0.85–1.15 before the request.
- Container is WAV; `playable_chunk()` gives each streamed chunk its own header
  so the renderer can decode chunks independently.

---

## Pipeline

```
  ┌──────────────┐   mic, 16 kHz mono
  │ Electron UI  │   renderer.js: pcmFrames() -> 1024-byte PCM frames
  └──────┬───────┘
         │ WebSocket  {type:"audio_chunk", format:"pcm16", rate:16000}
         ▼
  ┌──────────────────────────┐
  │ local_server.py          │  handle_audio_chunk -> _stream_pcm
  └──────┬───────────────────┘
         │ raw PCM, 1024 B/frame, one per 32 ms
         ▼
  ╔════════════════════════════════════════════╗
  ║  GNANI PRISMA v2.5                         ║
  ║  wss://api.vachana.ai/stt/v3/stream        ║
  ║  (REST POST /stt/v3 as the fallback)       ║
  ╚══════┬═════════════════════════════════════╝
         │ transcript
         ▼
  ┌──────────────────────────┐
  │ Agent: plan + act        │   <- Gemini (not Gnani)
  │ browser, files, apps     │
  └──────┬───────────────────┘
         │ answer text
         ▼
  ╔════════════════════════════════════════════╗
  ║  GNANI TIMBRE v2.5                         ║
  ║  POST /api/v1/tts/sse   (REST as fallback) ║
  ╚══════┬═════════════════════════════════════╝
         │ base64 WAV chunks, in chunk_index order
         ▼
  ┌──────────────┐
  │ Electron UI  │   decodeAudioData -> speakers
  └──────────────┘
```

---

## What is *not* Gnani

Stated plainly, because the speech claim above should be read precisely:

| Part | Technology | Where |
|---|---|---|
| Reasoning / planning | **Google Gemini** | `backend/providers/gemini.py`, `backend/providers/router.py` |
| Wake word ("Hey ClarityOS") | **Picovoice Porcupine** | `backend/servers/local_server.py` |
| Outbound phone calls | **Bolna** | `backend/tools/bolna_tools.py` |
| Browser control | ClarityOS Chrome extension (own code) | `chrome_extension/`, `backend/browser/` |

An optional `EvonProvider` (`backend/providers/evon.py`) can point the planner
at a self-hosted Gnani Evon v3.3 deployment. It is **off by default** and
assumes no hosted endpoint, because Gnani publishes the weights but not a
public inference URL. Gemini remains the default planner.

---

## Setup

```powershell
npm install                                   # 1. Node dependencies
bash setup.sh                                 # 2. Python venv + requirements
copy backend\.env.example backend\.env        # 3. Config
notepad backend\.env                          # 4. Add GEMINI_API_KEY and GNANI_API_KEY
npm start                                     # 5. Run
```

`GNANI_API_KEY` is required for voice. Without it the assistant still works by
text but cannot hear or speak — there is no other engine to fall back to.
Defaults are already `gnani-stream` (STT) and `gnani` (TTS); nothing else needs
changing to exercise the Gnani path.

---

## Verifying the integration

```powershell
# Gnani-only voice stack, factory behaviour, 429 handling, chunk ordering
PYTHONPATH=backend pytest tests/hub/test_voice_providers.py -q --asyncio-mode=auto

# No other speech vendor survives anywhere in the tree
grep -rniE "sarvam|speech_recognition|texttospeech|recognize_google|WindowsSapi" .
```

The second command returns only the settings-migration code in
`backend/hub/config.py` and the test assertions that check those classes no
longer exist.

---

## Team

Solo submission — **Nishtha Pandey**.

Sole author of everything in this repository: the agent loop, the Windows and
browser tooling, the Electron front end, and the Gnani Prisma and Timbre
integration described above.

---

## A note on numbers

This document contains no latency or accuracy figures. The Gnani integration
has been verified at the protocol level — endpoints, auth headers, frame sizes,
payload fields and event parsing all checked against Gnani's published
documentation and covered by tests — but it has not been benchmarked against a
live account, so there is nothing honest to report yet. Any figures the judges
need can be measured on a real key.
