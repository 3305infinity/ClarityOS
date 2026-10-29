"""
ClarityOS — Backend Server (Agentic)
=====================================
WebSocket server that:
  1. Receives audio from Electron → detects wake word → transcribes speech
  2. Passes transcribed text to the Agent Loop (perception + planning + tools)
  3. Streams UI state updates back to the Electron overlay
"""

import asyncio
import websockets
import json
import secrets
import base64
import wave
import io
import struct
import time
import numpy as np
import sys
import os
from functools import partial

from dotenv import load_dotenv

# Force print to flush immediately so Electron gets the logs in real-time
print = partial(print, flush=True)

# Ensure the backend package root is on sys.path so 'agent', 'tools', etc. resolve
# regardless of the working directory (Electron launches with cwd = project root).
_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# Must follow the sys.path setup above (this file runs as a script from
# backend/servers, so the backend root is not importable before it).
import compat_env  # noqa: E402,F401  (maps legacy env names before they are read)

load_dotenv(os.path.join(_backend_dir, ".env"))
# Per-user config (set by Electron): %APPDATA%\ClarityOS\.env on Windows, ~/Library/Application Support/ClarityOS/.env on macOS.
# Lets packaged builds keep GNANI_API_KEY / SARVAM_API_KEY / BOLNA_API_KEY outside the install folder.
_user_dir = os.environ.get("CLARITYOS_USER_DIR", "")
if _user_dir:
    load_dotenv(os.path.join(_user_dir, ".env"), override=True)

# Keys saved in the website's Settings → API keys live in Windows Credential
# Manager, which the hub loads at startup. This process is a separate one, so
# without this a BOLNA_API_KEY entered there would be invisible here and
# bolna_call would report "not configured". Gap-fill only: anything already set
# by a .env file above, or injected by the Electron main process, still wins.
try:
    from hub.config import SecretStore as _SecretStore
    _SecretStore().load_into_env(override=False)
except Exception as _e:        # keyring missing/locked — .env still works
    print(f"[Server] Credential Manager not available ({_e.__class__.__name__}); using .env only")

# Voice libraries (soft import — server still starts if missing, wake word is just disabled)
try:
    import pvporcupine
except ImportError:
    pvporcupine = None
    print("[Voice] pvporcupine not installed — wake word will be disabled")
from voice.stt import get_stt_provider, NoSpeechError, STTRequestError
from voice.stt import SAMPLE_RATE as STT_SAMPLE_RATE


def _resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int, channels: int = 1) -> bytes:
    """Linear-resample signed 16-bit PCM, mixing to mono first.

    Deliberately dependency-free (numpy is already a hard requirement): this
    runs per audio chunk on the voice path, and pulling in scipy/librosa for a
    rate conversion would be a far larger change than the bug warrants.
    """
    if not pcm or src_rate <= 0 or src_rate == dst_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return pcm
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    out_len = int(round(samples.size * (dst_rate / float(src_rate))))
    if out_len <= 0:
        return b""
    resampled = np.interp(
        np.linspace(0.0, samples.size - 1, out_len, dtype=np.float64),
        np.arange(samples.size, dtype=np.float64),
        samples.astype(np.float64),
    )
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


# ClarityOS Agent (V2 runtime)
from agent import create_agent
import agent.perception as perception
from runtime_state import runtime_state_store
from servers.voice_guard import install_voice_guard
from voice.progress_narrator import ProgressNarrator
from voice.tts import get_tts_engine, prepare_for_speech
from servers.browser_bridge_server import BRIDGE_HOST, BRIDGE_PORT, bridge_handler
from browser import BrowserResolver, browser_bridge, browser_store, ActionRequest
from browser.selector_ai import build_ranked_candidates, select_browser_candidate_with_flash

# Agent version toggle (deprecated compatibility env; V2 is always used)
AGENT_VERSION = os.environ.get("CLARITYOS_AGENT_VERSION", "v2")

# Picovoice access key (loaded from .env)
PICOVOICE_ACCESS_KEY = os.environ.get("PICOVOICE_ACCESS_KEY", "")

browser_resolver = BrowserResolver()


def _map_user_action_to_text(action: str) -> str:
    normalized = (action or "").strip().lower()
    if not normalized:
        return ""
    if normalized == "approve_plan":
        return "proceed"
    if normalized == "cancel_plan":
        return "cancel"
    return normalized


class VoiceAssistant:
    def __init__(self):
        self.state = "IDLE"  # IDLE, LISTENING, LOADING, DOING
        self.porcupine = None
        self.agent = create_agent(AGENT_VERSION)
        print("[Server] Agent initialized: V2")

        # Every tool call from this path goes through the same PolicyEngine the
        # website uses. Installed on the shared registry (idempotent), so tools
        # invoked deep inside the planner/executor are covered too.
        import tools as _tools
        self.guard = install_voice_guard(_tools.registry)

        # On Windows, rebind the desktop tools (open_app, get_ui_tree, type_text…)
        # onto this machine. Without this they stayed bound to the macOS
        # AppleScript implementations, so "Open Spotify" ran osascript and did
        # nothing. Installed *after* the policy guard, so every call is still
        # classified and still needs approval where the policy demands it.
        from servers.windows_local_tools import install_local_desktop_tools
        skipped = install_local_desktop_tools(_tools.registry)
        if skipped:
            print(f"[Server] Windows desktop tools not installed: {skipped}")
        else:
            print("[Server] Windows desktop tools installed (open_app, get_ui_tree, …)")
        
        # We try to initialize Porcupine, but it will fail if the key is default
        try:
            if pvporcupine is None:
                print("[Voice] ⚠ pvporcupine not available — wake word disabled")
            elif not PICOVOICE_ACCESS_KEY or PICOVOICE_ACCESS_KEY == "YOUR_PICOVOICE_ACCESS_KEY_HERE":
                print("[Voice] ⚠ Picovoice Access Key not set — wake word disabled")
                print(f"[Voice]   PICOVOICE_ACCESS_KEY = '{PICOVOICE_ACCESS_KEY[:8]}…' (len={len(PICOVOICE_ACCESS_KEY)})")
            else:
                # Look for the .ppn file in the project root (one level above _backend_dir)
                project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
                custom_ppn = os.path.join(project_root, "hey_clarityos.ppn")
                # Also check inside the servers directory (bundled copy)
                servers_ppn = os.path.join(os.path.dirname(__file__), "hey_clarityos.ppn")
                ppn_path = custom_ppn if os.path.exists(custom_ppn) else (servers_ppn if os.path.exists(servers_ppn) else None)

                print(f"[Voice] Picovoice key: {PICOVOICE_ACCESS_KEY[:12]}… (len={len(PICOVOICE_ACCESS_KEY)})")
                print(f"[Voice] PPn search: project_root={custom_ppn} exists={os.path.exists(custom_ppn)}")
                print(f"[Voice] PPn search: servers_dir={servers_ppn} exists={os.path.exists(servers_ppn)}")

                if ppn_path:
                    self.porcupine = pvporcupine.create(
                        access_key=PICOVOICE_ACCESS_KEY,
                        keyword_paths=[ppn_path]
                    )
                    print(f"[Voice] ✓ Porcupine initialized with 'Hey ClarityOS' from {ppn_path}")
                else:
                    self.porcupine = pvporcupine.create(
                        access_key=PICOVOICE_ACCESS_KEY,
                        keywords=["porcupine"]
                    )
                    print("[Voice] ⚠ hey_clarityos.ppn not found — using built-in 'Porcupine' keyword")
                    print(f"[Voice]   Searched: {custom_ppn}")
                    print(f"[Voice]   Searched: {servers_ppn}")
        except Exception as e:
            print(f"[Voice] ✗ Porcupine init failed: {e}")

        # For capturing the command after wake
        self.audio_buffer = bytearray()
        self.stt = get_stt_provider()  # google | gnani | sarvam (CLARITYOS_STT_PROVIDER)
        self.consecutive_silence_chunks = 0
        self.SILENCE_THRESHOLD_CHUNKS = 7  # Approx 0.45 seconds of sustained silence
        self.MIN_BUFFER_SIZE = 16000 * 2 * 0.3  # Min 0.3 seconds of audio
        self.grace_chunks_remaining = 0  # Grace period after await_reply
        self.waiting_for_voice = False  # True = require voice onset before recording
        self.waiting_for_reply = False  # True = do not abort on STT silence

        # Conversation mode: user can talk without saying the wake word each time
        self.conversation_mode = False
        self.conversation_mode_timeout = 120  # seconds of silence before auto-off
        self._conversation_timer = None

        # TTS state: mute mic while speaking to prevent feedback.
        # Released by the renderer's `tts_done`, or by the watchdog deadline.
        self.tts_playing = False
        self._tts_deadline = 0.0

        # Active agent task for interrupt support
        self._active_task: asyncio.Task | None = None
        self._active_websocket = None

        # Speaks brief progress updates at meaningful transitions. It reuses
        # _stream_tts, so updates go through the same gate/queue as any reply
        # and can never overlap one.
        self._narrator_ws = None
        self.narrator = ProgressNarrator(
            speak=self._speak_progress,
            is_speaking=lambda: self.tts_playing,
        )

    async def _speak_progress(self, line: str) -> None:
        """Narrator → TTS. Never speaks over an active sentence or after cancel."""
        ws = self._narrator_ws or self._active_websocket
        if ws is None or self.tts_playing:
            return
        if runtime_state_store.is_request_cancelled():
            return
        print(f"[Narrator] 🗣 {line}")
        await self._stream_tts(ws, line)

    async def run_agent_text(self, websocket, text: str):
        print(f"=> INPUT: {text}")

        self.narrator.reset()

        sent_response = {"done": False}

        async def ws_callback(msg: dict):
            if isinstance(msg, dict) and msg.get("type") in ("response", "action"):
                sent_response["done"] = True
            try:
                await websocket.send(json.dumps(msg))
            except Exception as e:
                print(f"[WS Callback] Error sending: {e}")
            # Same event stream the UI sees — the narrator only *selects* from
            # it, and speaking never blocks or alters the agent's progress.
            try:
                self._narrator_ws = websocket
                await self.narrator.consider(msg)
            except Exception as e:
                print(f"[Narrator] skipped an update ({e.__class__.__name__})")

        context = await perception.snapshot(text)

        # Pass conversation-mode flag so the agent can tailor responses
        self.agent._conversation_mode = self.conversation_mode

        # One budget per request, shared by every tool call. Cleared on every
        # exit path so a later request never inherits an expired deadline.
        from tools.registry import start_task_budget, clear_task_budget
        start_task_budget()
        try:
            result = await self.agent.run(text, context, ws_callback=ws_callback)
        finally:
            clear_task_budget()

        if isinstance(result, tuple):
            response_text, awaiting_reply = result
        else:
            response_text = str(result)
            awaiting_reply = False

        if "[CONVERSATION_MODE_ON]" in (response_text or ""):
            self.conversation_mode = True
            print("[Backend] 🗣 Conversation mode ENABLED")
            await websocket.send(json.dumps({
                "type": "conversation_mode", "enabled": True
            }))
        elif "[CONVERSATION_MODE_OFF]" in (response_text or ""):
            self.conversation_mode = False
            print("[Backend] 🔇 Conversation mode DISABLED")
            await websocket.send(json.dumps({
                "type": "conversation_mode", "enabled": False
            }))

        # Stream TTS for the response
        clean_response = (response_text or "").replace("[CONVERSATION_MODE_ON]", "").replace("[CONVERSATION_MODE_OFF]", "").strip()

        # Guarantee exactly one terminal response event per task. Some agent
        # paths emit their own (direct action, graph, milestone executor); the
        # conversational fast path never did, so its answer was spoken by TTS
        # while the UI waited forever for a response that was not coming.
        # Emit here only if nothing already did.
        if clean_response and not sent_response["done"]:
            await websocket.send(json.dumps({
                "type": "response",
                "payload": {"text": clean_response, "app": ""},
            }))
            sent_response["done"] = True

        if clean_response:
            await self._stream_tts(websocket, clean_response)

        if awaiting_reply:
            print("[Backend] Agent awaiting reply — listening without wake word")
            self.state = "LISTENING"
            self.audio_buffer = bytearray()
            self.consecutive_silence_chunks = 0
            self.grace_chunks_remaining = 64
            self.waiting_for_voice = True
            self.waiting_for_reply = True
            return

        if self.conversation_mode:
            print("[Backend] 🗣 Conversation mode — listening for next input")
            self.state = "LISTENING"
            self.audio_buffer = bytearray()
            self.consecutive_silence_chunks = 0
            self.grace_chunks_remaining = 48
            self.waiting_for_voice = True
            self.waiting_for_reply = False
            self._reset_conversation_timer()
            return

        self.waiting_for_reply = False
        self.state = "IDLE"
        self.audio_buffer = bytearray()

    # ── TTS Streaming ──

    async def _stream_tts(self, websocket, text: str):
        """Stream TTS audio sentence-by-sentence to the renderer."""
        if not text or len(text.strip()) < 3:
            return
        try:
            tts = get_tts_engine()
            speech_text = prepare_for_speech(text)
            if not speech_text:
                return

            self.tts_playing = True
            async for chunk in tts.stream_synthesize(speech_text):
                # to_ws_message() already returns serialized JSON — passing it
                # through json.dumps() again would make the renderer's single
                # JSON.parse() yield a string, so msg.type would be undefined
                # and the tts_chunk handler would never run.
                await websocket.send(chunk.to_ws_message())

            await websocket.send(json.dumps({"type": "tts_done"}))
            # Keep the mic gated until the RENDERER reports playback finished.
            # Clearing it here (the old behaviour) un-gated the mic the moment
            # the last chunk was *sent*, while the speakers still had seconds of
            # audio to play — so the mic captured our own voice, STT turned it
            # into a "user utterance", and the assistant echoed itself.
            self._arm_tts_watchdog()
        except Exception as e:
            print(f"[TTS] Streaming error: {e}")
            self.tts_playing = False          # nothing is playing — release now

    # ── TTS gate lifecycle ──
    #
    # `tts_playing` mutes the mic pipeline so the assistant never hears itself.
    # It is set when streaming starts and released by the renderer's `tts_done`.
    # The watchdog is the safety net: if the renderer never reports back (window
    # closed, audio device lost, decode failure), the mic must not stay dead.

    TTS_WATCHDOG_S = 90.0

    def _arm_tts_watchdog(self) -> None:
        self._tts_deadline = time.time() + self.TTS_WATCHDOG_S

    def _release_tts_gate(self, reason: str) -> None:
        if self.tts_playing:
            print(f"[TTS] mic gate released ({reason})")
        self.tts_playing = False
        self._tts_deadline = 0.0

    def _tts_gate_expired(self) -> bool:
        """True when playback should have finished but `tts_done` never arrived."""
        return bool(self.tts_playing and self._tts_deadline and time.time() > self._tts_deadline)

    # ── Interrupt / Cancel ──

    async def cancel_active_task(self, websocket=None):
        """Cancel the currently running agent task."""
        ws = websocket or self._active_websocket
        print("[Backend] ⛔ Cancelling active task")
        runtime_state_store.cancel_request()

        # Deny anything waiting for approval: a cancelled task must not leave a
        # consequential action that can still fire once someone clicks Approve.
        self.guard.cancel_pending("task cancelled")

        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            self._active_task = None

        # Drop any progress update that was waiting for the speakers to free up.
        self.narrator.cancel()

        # Stop any TTS playback on the renderer. Cancelling silences the
        # speakers immediately, so the mic gate must come off with it.
        self._release_tts_gate("task cancelled")
        if ws:
            try:
                await ws.send(json.dumps({"type": "tts_stop"}))
                await ws.send(json.dumps({"type": "status", "state": "state-idle"}))
            except Exception:
                pass

        self.state = "IDLE" if not self.conversation_mode else "LISTENING"

    # ── Conversation Mode ──

    def toggle_conversation_mode(self) -> bool:
        """Toggle conversation mode on/off. Returns the new state."""
        self.conversation_mode = not self.conversation_mode
        if self.conversation_mode:
            print("[Backend] 🗣 Conversation mode ENABLED (user toggle)")
            self._reset_conversation_timer()
        else:
            print("[Backend] 🔇 Conversation mode DISABLED (user toggle)")
            self._cancel_conversation_timer()
        return self.conversation_mode

    def _reset_conversation_timer(self):
        """Reset the auto-off timer for conversation mode."""
        self._cancel_conversation_timer()
        loop = asyncio.get_event_loop()
        self._conversation_timer = loop.call_later(
            self.conversation_mode_timeout,
            lambda: asyncio.ensure_future(self._conversation_timeout())
        )

    def _cancel_conversation_timer(self):
        """Cancel any pending conversation mode timeout."""
        if self._conversation_timer:
            self._conversation_timer.cancel()
            self._conversation_timer = None

    async def _conversation_timeout(self):
        """Auto-disable conversation mode after silence timeout."""
        if not self.conversation_mode:
            return
        print(f"[Backend] ⏰ Conversation mode auto-off after {self.conversation_mode_timeout}s silence")
        self.conversation_mode = False
        self._conversation_timer = None
        ws = self._active_websocket
        if ws:
            try:
                await ws.send(json.dumps({"type": "conversation_mode", "enabled": False}))
                if self.state != "DOING":
                    self.state = "IDLE"
                    await ws.send(json.dumps({"type": "status", "state": "state-idle"}))
            except Exception:
                pass

    async def handle_audio_chunk(self, websocket, b64_payload):
        """Decode base64 WAV, strip header, get raw PCM bytes."""
        # Mute mic pipeline while TTS is playing to prevent feedback.
        # The watchdog guarantees the gate cannot latch shut if the renderer
        # never reports playback finished.
        if self._tts_gate_expired():
            self._release_tts_gate("watchdog — renderer never sent tts_done")
        if self.tts_playing:
            return

        try:
            wav_bytes = base64.b64decode(b64_payload)
            
            # Use Python's wave module to read the WAV chunk
            with wave.open(io.BytesIO(wav_bytes), 'rb') as w:
                pcm_data = w.readframes(w.getnframes())
                chunk_rate = w.getframerate()
                chunk_channels = w.getnchannels()

            # Everything downstream (silence detection, the STT engines) assumes
            # 16 kHz mono. A browser is free to hand the renderer a 44.1/48 kHz
            # AudioContext even when 16 kHz was requested, and the resulting
            # audio is then transcribed at the wrong speed — which reads as
            # "no intelligible speech" rather than as a format error. Resample
            # rather than silently mis-transcribe.
            if chunk_rate and chunk_rate != STT_SAMPLE_RATE:
                if self._rate_warned != chunk_rate:
                    self._rate_warned = chunk_rate
                    print(f"[Audio] client is sending {chunk_rate} Hz / {chunk_channels}ch; "
                          f"resampling to {STT_SAMPLE_RATE} Hz mono for STT.")
                pcm_data = _resample_pcm16(pcm_data, chunk_rate, STT_SAMPLE_RATE, chunk_channels)

            if self.state == "IDLE":
                await self.process_wake_word(websocket, pcm_data)
            elif self.state == "LISTENING":
                await self.buffer_command(websocket, pcm_data)
                
        except Exception as e:
            print(f"Error processing audio chunk: {e}")

    async def process_wake_word(self, websocket, pcm_data):
        """Feed audio frames into Porcupine to detect wake word."""
        if not self.porcupine:
            return  # Can't detect without the engine

        chunk_size = 1024
        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i:i+chunk_size]
            if len(chunk) == chunk_size:
                pcm_tuple = struct.unpack_from("h" * self.porcupine.frame_length, chunk)
                
                keyword_index = self.porcupine.process(pcm_tuple)
                if keyword_index >= 0:
                    print("=> WAKE WORD DETECTED!")
                    self.state = "LISTENING"
                    self.audio_buffer = bytearray()
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    break

    async def buffer_command(self, websocket, pcm_data):
        """Buffer incoming audio while listening, detect silence to stop."""

        # Grace period: discard audio (don't buffer silence before user speaks)
        if self.grace_chunks_remaining > 0:
            self.grace_chunks_remaining -= 1
            return

        # Calculate RMS of the incoming chunk
        ints = np.frombuffer(pcm_data, dtype=np.int16)
        if len(ints) == 0:
            return
        rms = np.sqrt(np.mean(ints.astype(np.float32)**2))

        # Phase 1: Wait for voice onset (after await_reply grace period)
        # Don't buffer until we hear actual speech — avoids capturing
        # silence or quiet system audio (e.g. YouTube playing)
        if self.waiting_for_voice:
            if rms > 1500:  # Voice onset threshold (significantly higher to filter out speaker audio bleed)
                print(f"[Audio] Voice detected (RMS={rms:.0f}), recording...")
                self.waiting_for_voice = False
                self.audio_buffer.extend(pcm_data)
            return  # Skip until voice detected

        # Phase 2: Normal buffering + silence detection
        self.audio_buffer.extend(pcm_data)

        if rms < 250:
            self.consecutive_silence_chunks += 1
        else:
            self.consecutive_silence_chunks = 0

        if len(self.audio_buffer) > self.MIN_BUFFER_SIZE:
            if self.consecutive_silence_chunks >= self.SILENCE_THRESHOLD_CHUNKS:
                print(f"=> SUSTAINED SILENCE ({self.consecutive_silence_chunks} chunks). Processing Command...")
                self.state = "LOADING"
                self.consecutive_silence_chunks = 0
                await websocket.send(json.dumps({
                    "type": "progress", "state": "state-loading"
                }))
                
                # Start processing the audio asynchronously
                asyncio.create_task(self.transcribe_and_act(websocket))

    _rate_warned = 0          # last mismatching client rate we logged

    async def _safe_send(self, websocket, payload: dict) -> bool:
        """Send a UI event, tolerating a client that has already gone away.

        Dictation events are advisory: if the sidebar closed mid-utterance,
        failing to deliver one must not abort the transcription path.
        """
        try:
            await websocket.send(json.dumps(payload))
            return True
        except Exception as exc:
            print(f"[Voice] could not deliver '{payload.get('type')}': "
                  f"{exc.__class__.__name__}")
            return False

    async def transcribe_and_act(self, websocket):
        """
        Run Speech-to-Text on the buffered audio, then hand off
        to the agentic pipeline.
        """
        # Level/duration of what was actually captured. When STT reports "no
        # speech", this is the difference between "the mic was silent", "the
        # mic is very quiet" and "we only buffered a fraction of a second" —
        # none of which are distinguishable from the error message alone.
        self._last_capture = {}
        try:
            _a = np.frombuffer(bytes(self.audio_buffer), dtype=np.int16)
            if _a.size:
                _rms = float(np.sqrt(np.mean(_a.astype(np.float32) ** 2)))
                _peak = int(np.abs(_a).max())
                _secs = _a.size / float(STT_SAMPLE_RATE)
                _verdict = ("silent" if _peak < 150
                            else "very quiet" if _rms < 250
                            else "ok")
                self._last_capture = {"secs": _secs, "rms": _rms,
                                      "peak": _peak, "verdict": _verdict}
                print(f"[Audio] captured {_secs:.2f}s  rms={_rms:.0f}  peak={_peak} ({_verdict})")
        except Exception:
            pass

        print(f"Transcribing {len(self.audio_buffer)} bytes of audio...")

        # The sidebar drives its dictation UI off these three events (see the
        # "Voice input" note in sidebar/sidebar.js). They were never emitted, so
        # the mic button stayed in its recording state forever and the user
        # never saw what had been heard — even though transcription itself had
        # worked. Clients that do not know these types ignore them.
        await self._safe_send(websocket, {"type": "transcribing"})

        try:
            # Hand the raw 16 kHz mono PCM to the configured STT engine
            # (Google / Gnani Vachana / Sarvam). Raises NoSpeechError or STTRequestError.
            try:
                text = await self.stt.transcribe(bytes(self.audio_buffer))
                print(f"=> TRANSCRIBED: {text}")

                # Show the user what was heard before acting on it. Sent once,
                # and the client only displays it — the agent run below is the
                # single submission, so an utterance cannot be handled twice.
                await self._safe_send(websocket, {"type": "transcript", "text": text})
                
                # ════════════════════════════════════════════
                #  Voice interrupt: if agent is working and user
                #  says "stop"/"cancel", cancel the task instead
                #  of queueing a new agent run.
                # ════════════════════════════════════════════
                _cancel_words = {
                    "stop", "cancel", "never mind", "nevermind", "abort", "quit",
                    # Hindi / Hinglish
                    "ruko", "ruk jao", "band karo", "rehne do", "chhodo", "रुको", "रुक जाओ", "बंद करो", "रहने दो", "छोड़ो",
                }
                if self.state == "DOING" or self._active_task and not self._active_task.done():
                    normalized = text.strip().lower()
                    if normalized in _cancel_words or any(normalized.startswith(w) for w in _cancel_words):
                        print(f"[Backend] ⛔ Voice interrupt: '{text}' → cancelling task")
                        await self.cancel_active_task(websocket)
                        await websocket.send(json.dumps({
                            "type": "response",
                            "payload": {"text": "Stopped — let me know if you need anything else.", "app": ""}
                        }))
                        self.state = "IDLE" if not self.conversation_mode else "LISTENING"
                        if self.conversation_mode:
                            self.audio_buffer = bytearray()
                            self.consecutive_silence_chunks = 0
                            self.grace_chunks_remaining = 32
                            self.waiting_for_voice = True
                            self._reset_conversation_timer()
                            await websocket.send(json.dumps({"type": "status", "state": "state-listening"}))
                        return
                
                # ════════════════════════════════════════════
                #  AGENTIC PIPELINE — This is where the magic happens
                # ════════════════════════════════════════════
                
                await self.run_agent_text(websocket, text)
                if self.state != "IDLE":
                    return
                
            except NoSpeechError:
                print("STT: no intelligible speech in audio")
                # If we were in an await_reply loop or conversation mode, keep listening
                if getattr(self, "waiting_for_reply", False) or self.conversation_mode:
                    print("[Backend] Keeping microphone open (conversation mode or await reply)...")
                    self.state = "LISTENING"
                    self.audio_buffer = bytearray()
                    self.consecutive_silence_chunks = 0
                    self.grace_chunks_remaining = 0
                    self.waiting_for_voice = True
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    return
                # Otherwise, it was a wake word trigger that failed, reset to idle
                # "No speech" has three very different causes, and the generic
                # wording sent the user to the same Settings page regardless.
                cap = getattr(self, "_last_capture", {}) or {}
                verdict, secs = cap.get("verdict"), cap.get("secs", 0.0)
                if verdict == "silent":
                    message = "The microphone recorded silence."
                    hint = ("Windows is capturing nothing from it. Check it isn't muted, "
                            "and that the device you are speaking into is the default "
                            "under Settings → Sound → Input.")
                elif verdict == "very quiet":
                    message = "That was too quiet to make out."
                    hint = (f"I recorded {secs:.1f}s but the level was very low — raise the "
                            f"input volume under Settings → Sound → Input, or move closer "
                            f"to the mic.")
                elif secs and secs < 0.8:
                    message = "That recording was too short to transcribe."
                    hint = ("Only %.1fs was captured. Start speaking right after you press "
                            "Speak, and press it again once you have finished." % secs)
                else:
                    message = "I didn't catch any speech in that."
                    hint = ("Check that the right microphone is selected in "
                            "Windows Settings → Sound → Input, then try again.")
                await self._safe_send(websocket, {
                    "type": "transcript_error", "message": message, "hint": hint,
                })
                await websocket.send(json.dumps({
                    "type": "response",
                    "payload": {"text": "Sorry, I didn't catch that.", "app": ""}
                }))
                
            except STTRequestError as e:
                print(f"STT request failed: {e}")
                if self.conversation_mode:
                    print("[Backend] Network error but conversation mode on — keeping mic open")
                    self.state = "LISTENING"
                    self.audio_buffer = bytearray()
                    self.consecutive_silence_chunks = 0
                    self.grace_chunks_remaining = 16
                    self.waiting_for_voice = True
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    return
                # Keep the engine's own wording: it distinguishes a network
                # failure from a rejected key or an exhausted quota, which is
                # exactly what the user needs in order to fix it.
                await self._safe_send(websocket, {
                    "type": "transcript_error",
                    "message": "Speech recognition failed.",
                    "hint": str(e)[:200],
                })
                await websocket.send(json.dumps({
                    "type": "response",
                    "payload": {"text": "Network error processing speech.", "app": ""}
                }))

        except Exception as e:
            print(f"Transcription error: {e}")
            if self.conversation_mode:
                print("[Backend] Error but conversation mode on — keeping mic open")
                self.state = "LISTENING"
                self.audio_buffer = bytearray()
                self.consecutive_silence_chunks = 0
                self.grace_chunks_remaining = 16
                self.waiting_for_voice = True
                try:
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                except Exception:
                    pass
                return
            await websocket.send(json.dumps({
                "type": "response",
                "payload": {"text": "Internal error.", "app": ""}
            }))

        # Reset state
        self.state = "IDLE"
        self.audio_buffer = bytearray()


# ── Extension pairing through the Electron UI ───────────────────────────────
# This process also serves the :8765 extension bridge, but it has no hub window
# to show a pairing prompt in, so pairing_broker was left unconfigured and the
# popup reported "This backend cannot show a pairing prompt".
# It does have a UI though — the overlay and the sidebar — and both already
# render `approval_request` and answer with `approval_response`. Reuse that
# instead of inventing a second prompt channel. Approval is still required;
# nothing is granted automatically.
_ui_clients: list = []
_pending_pairings: dict = {}      # approval_id -> pairing request_id


async def _notify_pair_request(event: dict) -> None:
    if not _ui_clients:
        raise RuntimeError("no ClarityOS window is open")
    approval_id = f"pair-{event['request_id']}"
    _pending_pairings[approval_id] = event["request_id"]
    payload = json.dumps({
        "type": "approval_request",
        "approval_id": approval_id,
        "reason": f"“{event.get('extension_name') or 'A Chrome extension'}” wants to connect to ClarityOS.",
        "summary": "Approving lets it read and act on tabs you ask about. "
                   "Only approve a request you just started from the extension.",
        "timeout": event.get("timeout", 120),
    })
    # Newest window first; a closed socket must not sink the request.
    sent = False
    for ws in reversed(list(_ui_clients)):
        try:
            await ws.send(payload)
            sent = True
            break
        except Exception:
            continue
    if not sent:
        _pending_pairings.pop(approval_id, None)
        raise RuntimeError("the ClarityOS window did not accept the prompt")


def _resolve_pairing(approval_id: str, approved: bool) -> bool:
    """Returns True when this approval id belonged to a pairing request."""
    request_id = _pending_pairings.pop(approval_id, None)
    if request_id is None:
        return False
    from browser.pairing import pairing_broker
    pairing_broker.resolve(request_id, approved)
    return True


def _install_pairing_broker() -> None:
    from browser.bridge import browser_bridge
    from browser.pairing import pairing_broker
    pairing_broker.configure(_notify_pair_request, lambda: browser_bridge.session_token)


# ── Browser bridge owner surface over the existing :8000 listener ──
# When THIS process owns port 8765 (it started before the hub), the hub has no
# extension connection. websockets lets us answer plain HTTP on the same
# listener, so the owner surface is available either way round without adding a
# second server or a second port.
async def _browser_api_request(connection, request):
    path = request.path or ""
    if not path.startswith("/api/browser/"):
        return None                      # not ours: let the WebSocket upgrade proceed

    from http import HTTPStatus
    import urllib.parse as _up

    try:
        from hub.config import load_tokens
        expected = (load_tokens().get("ui") or "").strip()
    except Exception:
        expected = ""
    supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(supplied, expected):
        return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")

    parsed = _up.urlparse(path)
    query = _up.parse_qs(parsed.query)
    session_id = (query.get("session_id") or [""])[0]
    route = parsed.path

    from browser import owner_api

    async def _run():
        if route == "/api/browser/status":
            return owner_api.status()
        if route == "/api/browser/snapshot":
            return await owner_api.snapshot(session_id=session_id)
        if route == "/api/browser/page":
            return await owner_api.page(session_id=session_id)
        if route == "/api/browser/action":
            body = json.loads(request.body.decode("utf-8") or "{}") if request.body else {}
            return await owner_api.action(body.get("action") or {},
                                          timeout=float(body.get("timeout", 20.0) or 20.0))
        return {"ok": False, "error": "unknown_route", "message": route}

    # Awaited directly: process_request runs ON the event loop, so handing the
    # coroutine to run_coroutine_threadsafe and blocking on .result() deadlocked
    # the loop and the request never got a reply.
    try:
        payload = await asyncio.wait_for(_run(), timeout=30)
    except Exception as exc:
        payload = {"ok": False, "error": "owner_api_failed",
                   "message": f"{exc.__class__.__name__}: {exc}"}
    return connection.respond(HTTPStatus.OK, json.dumps(payload) + "\n")


_BROWSER_API_LOOP = None


GREETING_TEXT = "Hey! How can I help you today?"
_GREETED_THIS_PROCESS = False


async def main_handler(websocket):
    print("Electron App Connected!")
    assistant = VoiceAssistant()
    assistant._active_websocket = websocket
    _ui_clients.append(websocket)
    # Approval prompts go to this connection for as long as it lives.
    assistant.guard.attach(websocket)
    await assistant.agent.router.initialize()

    # Initialize UI
    try:
        await websocket.send(json.dumps({"type": "status", "state": "state-idle"}))

        # Speak the fixed startup greeting once per backend process, to the
        # first UI that connects. Per-process (not per-connection) so a
        # renderer reload, a sidebar reopen or the overlay connecting too does
        # not say it again. No model call: the text is fixed, and it rides the
        # existing tts_chunk/tts_done protocol. Display of the greeting is the
        # client's own concern and happens regardless of TTS.
        global _GREETED_THIS_PROCESS
        greeting_task = None
        if not _GREETED_THIS_PROCESS:
            _GREETED_THIS_PROCESS = True

            async def _speak_greeting():
                try:
                    await assistant._stream_tts(websocket, GREETING_TEXT)
                except Exception as exc:
                    print(f"[Server] Greeting not spoken ({exc.__class__.__name__}); "
                          f"the sidebar still shows it.")

            # Fire and forget. Awaiting this here blocked the message loop
            # below, so anything the user typed sat queued until the greeting
            # audio had finished streaming — their question looked ignored and
            # the greeting arrived late on top of it.
            greeting_task = asyncio.create_task(_speak_greeting())
        
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get("type")

                # The moment the user actually says something, the greeting is
                # no longer wanted — stop speaking over them.
                if greeting_task is not None and msg_type in ("text_input", "audio_chunk", "user_action"):
                    if not greeting_task.done():
                        greeting_task.cancel()
                        try:
                            await websocket.send(json.dumps({"type": "tts_stop"}))
                        except Exception:
                            pass
                    greeting_task = None
                
                if msg_type == "audio_chunk":
                    await assistant.handle_audio_chunk(websocket, data.get("payload", ""))
                elif msg_type == "cancel_task":
                    await assistant.cancel_active_task(websocket)
                elif msg_type == "toggle_conversation_mode":
                    enabled = assistant.toggle_conversation_mode()
                    await websocket.send(json.dumps({
                        "type": "conversation_mode", "enabled": enabled
                    }))
                    if enabled:
                        # Immediately start listening (no wake word needed)
                        assistant.state = "LISTENING"
                        assistant.audio_buffer = bytearray()
                        assistant.consecutive_silence_chunks = 0
                        assistant.grace_chunks_remaining = 32
                        assistant.waiting_for_voice = True
                        await websocket.send(json.dumps({
                            "type": "status", "state": "state-listening"
                        }))
                    else:
                        assistant.state = "IDLE"
                        await websocket.send(json.dumps({
                            "type": "status", "state": "state-idle"
                        }))
                elif msg_type == "approval_response":
                    approval_id = str(data.get("approval_id", ""))
                    approve = bool(data.get("approve", False))
                    # Extension pairing rides the same prompt; settle it first.
                    if _resolve_pairing(approval_id, approve):
                        continue
                    # Only a live, unexpired id can approve, and only once.
                    settled = assistant.guard.resolve(
                        str(data.get("approval_id", "")), bool(data.get("approve", False)))
                    if not settled:
                        print("[VoiceGuard] ignored an unknown/stale approval id")
                elif msg_type == "tts_done":
                    # Renderer finished playing all TTS audio — the only
                    # authoritative signal that the speakers have gone quiet.
                    assistant._release_tts_gate("renderer finished playback")
                elif msg_type == "text_input":
                    text = (data.get("text") or "").strip()
                    if text:
                        assistant.state = "LOADING"
                        await websocket.send(json.dumps({
                            "type": "progress", "state": "state-loading"
                        }))
                        await assistant.run_agent_text(websocket, text)
                elif msg_type == "user_action":
                    mapped_text = _map_user_action_to_text(data.get("action", ""))
                    if mapped_text:
                        assistant.state = "LOADING"
                        await websocket.send(json.dumps({
                            "type": "progress", "state": "state-loading"
                        }))
                        await assistant.run_agent_text(websocket, mapped_text)
                elif msg_type == "browser_debug_action":
                    query = (data.get("query") or "").strip()
                    action = (data.get("action") or "click").strip().lower()
                    session_id = (data.get("session_id") or "").strip()
                    text = data.get("text") or ""
                    option = data.get("option") or ""
                    clear_first = bool(data.get("clear_first", False))
                    timeout = float(data.get("timeout", 8.0) or 8.0)

                    if not query:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": "query is required",
                        }))
                        continue

                    snapshot = browser_store.get_snapshot(session_id or None)
                    if not snapshot:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": "No active browser snapshot is available.",
                        }))
                        continue

                    candidate = browser_resolver.best_candidate(query, snapshot.elements, action=action)
                    if not candidate:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": f"No browser candidate matched query '{query}' for action '{action}'.",
                            "session_id": snapshot.session_id,
                            "generation": snapshot.generation,
                        }))
                        continue

                    request = ActionRequest(
                        action=action,
                        ref_id=candidate.ref_id,
                        session_id=snapshot.session_id,
                        text=text,
                        option=option,
                        clear_first=clear_first,
                        timeout=timeout,
                    )
                    queued = browser_bridge.queue_action(request)
                    if not queued.ok:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": queued.message,
                            "query": query,
                            "candidate": {
                                "ref_id": candidate.ref_id,
                                "label": candidate.primary_label(),
                                "role": candidate.role or candidate.tag,
                            },
                        }))
                        continue

                    result = await browser_bridge.wait_for_result(queued.action_id, timeout=timeout)

                    if result is None:
                        await websocket.send(json.dumps({
                            "type": "browser_debug_result",
                            "ok": False,
                            "message": f"Timed out waiting for browser action result for '{query}'.",
                            "query": query,
                            "action": action,
                            "action_id": queued.action_id,
                            "candidate": {
                                "ref_id": candidate.ref_id,
                                "label": candidate.primary_label(),
                                "role": candidate.role or candidate.tag,
                            },
                        }))
                        continue

                    await websocket.send(json.dumps({
                        "type": "browser_debug_result",
                        "ok": result.ok,
                        "message": result.message,
                        "query": query,
                        "action": action,
                        "action_id": result.action_id,
                        "session_id": result.session_id,
                        "pre_generation": result.pre_generation,
                        "post_generation": result.post_generation,
                        "candidate": {
                            "ref_id": candidate.ref_id,
                            "label": candidate.primary_label(),
                            "role": candidate.role or candidate.tag,
                        },
                        "details": result.details,
                    }))
                elif msg_type == "browser_flash_action":
                    query = (data.get("query") or "").strip()
                    action = (data.get("action") or "click").strip().lower()
                    session_id = (data.get("session_id") or "").strip()
                    text = data.get("text") or ""
                    option = data.get("option") or ""
                    clear_first = bool(data.get("clear_first", False))
                    timeout = float(data.get("timeout", 10.0) or 10.0)

                    snapshot, _, snapshot_error = build_ranked_candidates(query, action, session_id=session_id, limit=8)
                    if not snapshot:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": snapshot_error or "No active browser snapshot is available.",
                        }))
                        continue

                    selection, error = await select_browser_candidate_with_flash(
                        query=query,
                        action=action,
                        session_id=snapshot.session_id,
                        text=text,
                        option=option,
                    )
                    if not selection:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": error,
                            "query": query,
                            "action": action,
                            "session_id": snapshot.session_id,
                            "generation": snapshot.generation,
                        }))
                        continue

                    candidate = browser_store.get_element(selection["ref_id"], snapshot.session_id)
                    if not candidate:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": f"Selected ref '{selection['ref_id']}' is no longer present.",
                            "selection": selection,
                        }))
                        continue

                    request = ActionRequest(
                        action=action,
                        ref_id=candidate.ref_id,
                        session_id=snapshot.session_id,
                        text=text,
                        option=option,
                        clear_first=clear_first,
                        timeout=timeout,
                    )
                    queued = browser_bridge.queue_action(request)
                    if not queued.ok:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": queued.message,
                            "selection": selection,
                        }))
                        continue

                    result = await browser_bridge.wait_for_result(queued.action_id, timeout=timeout)

                    if result is None:
                        await websocket.send(json.dumps({
                            "type": "browser_flash_result",
                            "ok": False,
                            "message": f"Timed out waiting for Gemini Flash browser action '{query}'.",
                            "action_id": queued.action_id,
                            "selection": selection,
                        }))
                        continue

                    await websocket.send(json.dumps({
                        "type": "browser_flash_result",
                        "ok": result.ok,
                        "message": result.message,
                        "query": query,
                        "action": action,
                        "action_id": result.action_id,
                        "session_id": result.session_id,
                        "pre_generation": result.pre_generation,
                        "post_generation": result.post_generation,
                        "selection": selection,
                        "details": result.details,
                    }))
                    
                elif msg_type == "hotkey_pressed":
                    print("=> PUSH-TO-TALK. Listening without wake word...")
                    assistant.state = "LISTENING"
                    assistant.audio_buffer = bytearray()
                    # Deliberate push-to-talk: the user has already said they
                    # are about to speak. Clear the gates left behind by an
                    # await_reply turn — otherwise the first 64 chunks were
                    # discarded as "grace" and recording then waited for a loud
                    # voice onset, so a normal-volume sentence recorded nothing.
                    assistant.grace_chunks_remaining = 0
                    assistant.waiting_for_voice = False
                    assistant.consecutive_silence_chunks = 0
                    assistant._release_tts_gate("push-to-talk")
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))
                    
            except json.JSONDecodeError:
                pass
            except Exception as inner_e:
                print(f"Error handling message: {inner_e}")
                
    except websockets.exceptions.ConnectionClosed as e:
        print(f"Electron disconnected: {e}")
    except Exception as e:
        print(f"Unexpected websocket error: {e}")
    finally:
        # The window is gone: deny anything still awaiting approval so a pending
        # consequential action cannot survive the UI that was meant to confirm it.
        assistant.guard.detach(websocket)
        try:
            _ui_clients.remove(websocket)
        except ValueError:
            pass


async def main():
    if PICOVOICE_ACCESS_KEY == "YOUR_PICOVOICE_ACCESS_KEY_HERE":
        print("!" * 60)
        print("ACTION REQUIRED: You must set PICOVOICE_ACCESS_KEY in ")
        print("backend_server.py to enable the wake word.")
        print("!" * 60)

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        print("!" * 60)
        print("NOTE: GEMINI_API_KEY not set. Agent will run in fallback mode.")
        print("Set it with: export GEMINI_API_KEY='your-key-here'")
        print("!" * 60)
        
    global _BROWSER_API_LOOP
    _BROWSER_API_LOOP = asyncio.get_running_loop()

    async with websockets.serve(
        main_handler,
        "127.0.0.1",
        8000,
        origins=None,
        process_request=_browser_api_request,
        ping_interval=120,
        ping_timeout=600,
    ):
        # The extension bridge is a secondary duty: the hub serves the same
        # bridge on the same port, so when the hub is already running this bind
        # fails. It used to raise straight out of main(), killing the whole
        # process — which took :8000 down with it and left every ClarityOS
        # window reporting "Offline". Keep serving the voice socket instead.
        bridge_server = None
        try:
            bridge_server = await websockets.serve(
                bridge_handler,
                BRIDGE_HOST,
                BRIDGE_PORT,
                origins=None,
                ping_interval=120,
                ping_timeout=600,
            )
        except OSError as exc:
            print(f"[Backend] Extension bridge not started on {BRIDGE_HOST}:{BRIDGE_PORT} "
                  f"({exc.__class__.__name__}) — the ClarityOS hub is already serving it.")

        try:
            if bridge_server is not None:
                _install_pairing_broker()
                print(f"Browser bridge running on ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
            print("Server running on ws://127.0.0.1:8000 (Allow All Origins)")
            print("[Backend] READY")
            await asyncio.Future()
        finally:
            if bridge_server is not None:
                bridge_server.close()


if __name__ == "__main__":
    asyncio.run(main())
