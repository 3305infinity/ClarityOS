"""
ClarityOS Hub core
=================
One shared brain for every front-end:

    Website  --WS /ws-->  Hub  <--WS /helper--  Windows helper   (desktop actions)
                           |
                           +--WS :8765--  Chrome extension        (browser actions)

The Hub owns: the SPAV agent, the single tool registry, the policy engine, the task store,
the approval queue, speech-to-text / text-to-speech and cancellation. Front-ends only send
commands and render events.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import io
import json
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from hub import config as cfg
from hub.approval import APPROVAL_TIMEOUT_S, ApprovalBroker
from hub.helper_link import HelperLink
from hub.policy import PolicyEngine, looks_like_captcha
from hub.redact import make_redactor
from hub.store import TaskStore

CURRENT_TASK: contextvars.ContextVar[str] = contextvars.ContextVar("clarityos_task", default="")

MAX_UTTERANCE_BYTES = 30 * 16000 * 2 + 4096     # 30 s of 16 kHz 16-bit mono
MIN_UTTERANCE_RMS = 120                          # below this it's silence/room noise — skip the STT call


class BusyError(RuntimeError):
    pass


class Client:
    """A connected website tab. `send_json` is provided by the transport (FastAPI WebSocket)."""

    def __init__(self, send_json: Callable[[Dict[str, Any]], Awaitable[None]]):
        self.id = uuid.uuid4().hex[:8]
        self.send_json = send_json
        self.authenticated = False


def _is_error_result(res: Any) -> bool:
    if not isinstance(res, str):
        return False
    r = res.lstrip()
    return r.startswith(("ERROR", "BLOCKED", "DENIED", "{'ok': False", '{"ok": false'))


def pcm_from_audio(raw: bytes) -> bytes:
    """Accept a WAV (16 kHz, mono, 16-bit) or raw PCM16 and return raw PCM16 bytes."""
    if raw[:4] == b"RIFF":
        with wave.open(io.BytesIO(raw), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
                raise ValueError("audio must be 16 kHz mono 16-bit")
            return w.readframes(w.getnframes())
    return raw


def rms_of(pcm: bytes) -> float:
    import numpy as np
    if len(pcm) < 2:
        return 0.0
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class Hub:
    def __init__(self, data_dir: Optional[Path] = None, agent_factory: Optional[Callable[[], Any]] = None,
                 use_keyring: bool = True):
        self.dir = Path(data_dir) if data_dir else cfg.user_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings = cfg.Settings(self.dir / "settings.json")
        self.secrets = cfg.SecretStore(use_keyring=use_keyring)
        self.secrets.load_into_env()
        cfg.apply_settings_to_env(self.settings.get())
        self.tokens = cfg.load_tokens() if data_dir is None else self._tokens_in(self.dir)
        self.redact = make_redactor(self.secrets.values)
        self.store = TaskStore(self.dir / "hub.db", self.redact)
        self.policy = PolicyEngine(self.settings.get)
        self.helper = HelperLink(on_change=self.broadcast_connections, on_emergency_stop=self._on_helper_stop)
        self.clients: Set[Client] = set()
        self._agent_factory = agent_factory
        self.agent: Any = None
        self.active: Optional[asyncio.Task] = None
        self.active_task_id = ""
        self.paused = False
        self._resume = asyncio.Event()
        self._resume.set()
        self.approvals = ApprovalBroker(APPROVAL_TIMEOUT_S)
        self._speak_task: Optional[asyncio.Task] = None
        self.speaking = False
        self._last_conn_sig = ""

    @staticmethod
    def _tokens_in(d: Path) -> Dict[str, str]:
        import os
        old = os.environ.get("CLARITYOS_USER_DIR")
        os.environ["CLARITYOS_USER_DIR"] = str(d)
        try:
            return cfg.load_tokens()
        finally:
            if old is None:
                os.environ.pop("CLARITYOS_USER_DIR", None)
            else:
                os.environ["CLARITYOS_USER_DIR"] = old

    # ═══════════════════ events ═══════════════════

    async def broadcast(self, event: Dict[str, Any], persist: bool = True) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        tid = event.get("task_id") or self.active_task_id
        if tid:
            event.setdefault("task_id", tid)
        if persist and tid and event.get("type") not in ("tts_chunk", "connections", "pong"):
            self.store.add_event(tid, event["type"], {k: v for k, v in event.items() if k not in ("type", "ts")})
        dead = []
        for c in list(self.clients):
            if not c.authenticated:
                continue
            try:
                await c.send_json(event)
            except Exception:
                dead.append(c)
        for c in dead:
            self.clients.discard(c)

    def connections(self) -> Dict[str, Any]:
        try:
            from browser import browser_bridge
            browser = {"connected": bool(browser_bridge.is_connected()),
                       "name": getattr(browser_bridge, "extension_name", "") or ""}
            if callable(browser["name"]):
                browser["name"] = browser["name"]()
        except Exception:
            browser = {"connected": False, "name": ""}
        s = self.settings.get()
        sec = self.secrets.configured()
        return {
            "browser": browser,
            "helper": self.helper.status(),
            "voice": {
                "stt_provider": s["stt_provider"], "tts_provider": s["tts_provider"],
                "stt_ready": s["stt_provider"] == "google" or sec.get({"gnani": "GNANI_API_KEY", "sarvam": "SARVAM_API_KEY"}.get(s["stt_provider"], ""), False),
                "tts_ready": s["tts_provider"] == "google" or sec.get({"gnani": "GNANI_API_KEY", "sarvam": "SARVAM_API_KEY"}.get(s["tts_provider"], ""), False),
            },
            "brain_ready": sec.get("GEMINI_API_KEY", False),
            "busy": bool(self.active and not self.active.done()),
            "paused": self.paused,
            "secrets_persistent": self.secrets.persistent,
        }

    async def broadcast_connections(self, force: bool = False) -> None:
        conn = self.connections()
        sig = json.dumps(conn, sort_keys=True, default=str)
        if force or sig != self._last_conn_sig:
            self._last_conn_sig = sig
            await self.broadcast({"type": "connections", "connections": conn}, persist=False)

    # ═══════════════════ guard (policy + approval + pause + events) ═══════════════════

    def install_guard(self, registry) -> None:
        # idempotent: always wrap the ORIGINAL execute, never a previous guard (restart / reload / tests)
        orig = getattr(registry, "_mw_orig_execute", None) or registry.execute
        registry._mw_orig_execute = orig
        hub = self

        async def guarded(name: str, args: Dict[str, Any]) -> str:
            tid = CURRENT_TASK.get() or hub.active_task_id
            safe_args = {k: v for k, v in (args or {}).items() if k != "reasoning"}
            # pause (checked between tool calls)
            if hub.paused:
                await hub.broadcast({"type": "paused", "task_id": tid})
                await hub._resume.wait()
            decision = hub.policy.check(name, args or {})
            if decision.action == "deny":
                await hub.broadcast({"type": "tool_blocked", "task_id": tid, "tool": name,
                                     "capability": decision.capability, "reason": decision.reason})
                from tools.contracts import error_envelope
                return str(error_envelope(code="policy.denied", message=f"Blocked by user policy: {decision.reason}"))
            if decision.action == "ask":
                approved = await hub.request_approval(tid, name, decision, args or {})
                if not approved:
                    from tools.contracts import error_envelope
                    denied = str(error_envelope(code="policy.user_denied",
                                                message="The user did not approve this action. Do not retry it; ask what they prefer."))
                    await hub.broadcast({"type": "tool_result", "task_id": tid, "tool": name, "ok": False, "ms": 0,
                                         "preview": denied[:300], "denied": True})
                    return denied
            await hub.broadcast({"type": "tool_start", "task_id": tid, "tool": name,
                                 "args": hub.redact(safe_args), "reason": hub.redact((args or {}).get("reasoning", ""))})
            t0 = time.monotonic()
            result = await orig(name, args)
            ms = int((time.monotonic() - t0) * 1000)
            note = ""
            if decision.capability == "browser" and isinstance(result, str) and looks_like_captcha(result):
                note = ("\n[CLARITYOS NOTE: a CAPTCHA / human-verification step is on this page. Do NOT try to bypass it. "
                        "Tell the user and ask them to complete it, then continue.]")
                await hub.broadcast({"type": "captcha_detected", "task_id": tid})
            await hub.broadcast({"type": "tool_result", "task_id": tid, "tool": name, "ok": not _is_error_result(result),
                                 "ms": ms, "preview": hub.redact(str(result))[:600]})
            return (result + note) if note and isinstance(result, str) else result

        registry.execute = guarded

    async def request_approval(self, tid: str, tool: str, decision, args: Dict[str, Any]) -> bool:
        return await self.approvals.request(
            tool=tool,
            capability=decision.capability,
            reason=decision.reason,
            summary=self.redact(decision.summary)[:500],
            task_id=tid,
            emit=self.broadcast,
        )

    def resolve_approval(self, approval_id: str, approve: bool) -> bool:
        return self.approvals.resolve(approval_id, approve)

    # ═══════════════════ agent / tasks ═══════════════════

    async def _ensure_agent(self) -> Any:
        if self.agent is None:
            if self._agent_factory:
                self.agent = self._agent_factory()
            else:
                from agent import create_agent
                self.agent = create_agent("v2")
                await self.agent.router.initialize()
        return self.agent

    def reset_agent(self) -> None:
        """New conversation (also picks up a changed Gemini key)."""
        self.agent = None

    def busy(self) -> bool:
        return bool(self.active and not self.active.done())

    async def start_task(self, text: str, source: str = "text", workflow_id: str = "") -> str:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty command")
        if len(text) > 4000:
            raise ValueError("command too long")
        if self.busy():
            raise BusyError("A task is already running. Cancel it first or wait for it to finish.")
        tid = self.store.create_task(text, source, workflow_id)
        self.active_task_id = tid
        self.active = asyncio.create_task(self._run(tid, text, source))
        return tid

    async def _run(self, tid: str, text: str, source: str) -> None:
        CURRENT_TASK.set(tid)
        status, summary = "done", ""
        await self.broadcast({"type": "task_started", "task_id": tid, "text": self.redact(text), "source": source})
        try:
            from runtime_state import runtime_state_store
            runtime_state_store.update_request_state(pending_confirmation="")   # clear a stale "cancelled" flag
            await self.helper.arm()
            agent = await self._ensure_agent()
            import agent.perception as perception
            context = await perception.snapshot(text)

            async def ws_callback(msg: Dict[str, Any]) -> None:
                await self._on_agent_message(tid, msg)

            result = await agent.run(text, context, ws_callback=ws_callback)
            response_text, awaiting = (result if isinstance(result, tuple) else (str(result), False))
            clean = (response_text or "").replace("[CONVERSATION_MODE_ON]", "").replace("[CONVERSATION_MODE_OFF]", "").strip()
            summary = clean[:500]
            if awaiting:
                await self.broadcast({"type": "await_input", "task_id": tid})
            if clean and self.settings.get().get("tts_enabled", True):
                await self._speak(clean)
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as e:  # surface a friendly, actionable error
            status = "error"
            msg, hint = self._friendly_error(e)
            summary = msg
            await self.broadcast({"type": "error", "task_id": tid, "message": msg, "hint": hint})
        finally:
            self.store.finish_task(tid, status, summary)
            await self.broadcast({"type": "task_finished", "task_id": tid, "status": status, "summary": self.redact(summary)}, persist=True)
            await self.broadcast_connections(force=True)

    def _friendly_error(self, e: Exception) -> tuple[str, str]:
        text = str(e)
        low = text.lower()
        if "api key" in low or "gemini_api_key" in low or "no gemini models" in low:
            return ("The AI brain isn't configured.", "Open Settings → API keys and add your Gemini key.")
        if "quota" in low or "429" in low or "rate" in low:
            return ("The AI provider is rate-limiting requests.", "Wait a minute and try again, or switch model/provider.")
        if "timeout" in low or "timed out" in low:
            return ("The step took too long and was stopped.", "Try again; if it repeats, simplify the task.")
        return (f"Something went wrong: {self.redact.string(text)[:300] if hasattr(self.redact, 'string') else text[:300]}",
                "Check Connections (Chrome / Windows helper) and try again.")

    async def _on_agent_message(self, tid: str, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "thinking":
            await self.broadcast({"type": "step", "task_id": tid, "kind": "thinking", "text": "Soch raha hoon… / Thinking…"})
        elif t in ("doing", "ack"):
            await self.broadcast({"type": "step", "task_id": tid, "kind": t, "text": self.redact(msg.get("text", "")),
                                  "tool": msg.get("tool", ""), "app": msg.get("app", "")})
        elif t in ("response", "action"):
            p = msg.get("payload") or {}
            await self.broadcast({"type": "response", "task_id": tid,
                                  "text": self.redact(p.get("text") or p.get("message") or ""),
                                  "modal": self.redact(p.get("modal_data") or p.get("modal") or None),
                                  "await_input": bool(p.get("await_input", False))})
        # progress / status / conversation_mode are UI chrome of the desktop overlay — ignored here

    # ═══════════════════ speech ═══════════════════

    async def _speak(self, text: str) -> None:
        from voice.tts import get_tts_engine, prepare_for_speech
        speech = prepare_for_speech(text)
        if not speech or len(speech.strip()) < 3:
            return
        self.speaking = True
        await self.broadcast({"type": "speaking", "state": True}, persist=False)

        async def run():
            try:
                engine = get_tts_engine()
                async for chunk in engine.stream_synthesize(speech):
                    mime = "audio/wav" if chunk.audio[:4] == b"RIFF" else ("audio/ogg" if chunk.audio[:4] == b"OggS" else "audio/mpeg")
                    await self.broadcast({"type": "tts_chunk", "seq": chunk.seq, "final": chunk.final, "mime": mime,
                                          "text": chunk.text, "data": base64.b64encode(chunk.audio).decode("ascii")}, persist=False)
                await self.broadcast({"type": "tts_done"}, persist=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self.broadcast({"type": "voice_warning", "message": f"Voice output failed: {self.redact.string(str(e))[:200]}. Showing text only."}, persist=False)
            finally:
                self.speaking = False
                await self.broadcast({"type": "speaking", "state": False}, persist=False)

        self._speak_task = asyncio.create_task(run())
        try:
            await self._speak_task
        except asyncio.CancelledError:
            if not (self._speak_task and self._speak_task.cancelled()):
                raise      # the *caller* was cancelled, not just the speech

    async def stop_speaking(self) -> None:
        t = self._speak_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.speaking = False
        await self.broadcast({"type": "tts_stop"}, persist=False)

    async def handle_utterance(self, audio_b64: str, language: str = "") -> None:
        """Voice command from the browser: STT -> transcript event -> task."""
        from voice.stt import get_stt_provider, NoSpeechError, STTRequestError
        try:
            raw = base64.b64decode(audio_b64, validate=True)
            if len(raw) > MAX_UTTERANCE_BYTES + 100:
                raise ValueError("recording too long (max 30 s)")
            pcm = pcm_from_audio(raw)
        except Exception as e:
            await self.broadcast({"type": "transcript_error", "kind": "bad_audio",
                                  "message": f"Audio could not be read ({e}). Please try again or type your command."})
            return
        if rms_of(pcm) < MIN_UTTERANCE_RMS or len(pcm) < 16000 * 2 * 0.25:
            await self.broadcast({"type": "transcript_error", "kind": "no_speech",
                                  "message": "Awaaz bahut dheemi thi / Too quiet to hear. Move closer to the mic or type instead."})
            return
        await self.broadcast({"type": "transcribing"}, persist=False)
        try:
            text = await get_stt_provider().transcribe(pcm)
        except NoSpeechError:
            await self.broadcast({"type": "transcript_error", "kind": "no_speech",
                                  "message": "Samajh nahi aaya / Couldn't make out any speech. Try again in a quieter place, or type."})
            return
        except STTRequestError as e:
            await self.broadcast({"type": "transcript_error", "kind": "stt_unavailable",
                                  "message": f"Speech recognition is unavailable ({self.redact.string(str(e))[:160]}). You can type your command instead.",
                                  "hint": "Check Settings → Voice and your API key."})
            return
        text = text.strip()
        await self.broadcast({"type": "transcript", "text": self.redact(text), "final": True,
                              "provider": self.settings.get()["stt_provider"]})
        try:
            await self.start_task(text, "voice")
        except BusyError as e:
            await self.broadcast({"type": "error", "message": str(e), "hint": "Press Stop, then speak again."})

    # ═══════════════════ control ═══════════════════

    async def cancel(self, reason: str = "user") -> None:
        """Stop everything, everywhere: agent task, speech, approvals, helper, extension queue."""
        try:
            from runtime_state import runtime_state_store
            runtime_state_store.cancel_request()
        except Exception:
            pass
        self.approvals.cancel_all(reason)
        self.paused = False
        self._resume.set()
        await self.stop_speaking()
        await self.helper.cancel_all(reason)
        try:
            from browser import browser_bridge
            browser_bridge.clear_pending_actions()
        except Exception:
            pass
        if self.active and not self.active.done():
            self.active.cancel()
            try:
                await self.active
            except (asyncio.CancelledError, Exception):
                pass
        await self.broadcast({"type": "cancelled", "reason": reason}, persist=False)
        await self.broadcast_connections(force=True)

    async def _on_helper_stop(self) -> None:
        await self.cancel("helper kill-switch")

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self._resume.clear()
        else:
            self._resume.set()
        await self.broadcast({"type": "paused" if paused else "resumed"}, persist=False)
        await self.broadcast_connections(force=True)

    async def replay_workflow(self, wid: str) -> None:
        wf = self.store.get_workflow(wid)
        if not wf:
            raise ValueError("workflow not found")

        async def runner():
            for i, step in enumerate(wf["steps"], 1):
                await self.broadcast({"type": "workflow_step", "workflow": wf["name"], "index": i, "total": len(wf["steps"]), "text": step}, persist=False)
                await self.start_task(step, "workflow", wid)
                await self.active
                last = self.store.list_tasks(1)[0]
                if last["status"] != "done":
                    await self.broadcast({"type": "workflow_stopped", "workflow": wf["name"], "at": i, "status": last["status"]}, persist=False)
                    return
            await self.broadcast({"type": "workflow_done", "workflow": wf["name"]}, persist=False)

        asyncio.create_task(runner())

    # ═══════════════════ website messages ═══════════════════

    async def handle_client_message(self, client: Client, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        try:
            if t == "command":
                await self.start_task(str(msg.get("text", "")), "text")
            elif t == "voice_utterance":
                asyncio.create_task(self.handle_utterance(str(msg.get("audio", "")), str(msg.get("language", ""))))
            elif t == "cancel":
                await self.cancel("user")
            elif t == "pause":
                await self.set_paused(True)
            elif t == "resume":
                await self.set_paused(False)
            elif t == "stop_speaking":
                await self.stop_speaking()
            elif t == "approval_response":
                self.resolve_approval(str(msg.get("approval_id", "")), bool(msg.get("approve", False)))
            elif t == "bridge_pair_response":
                # Approve/Deny for a Chrome extension asking to pair. Only this
                # authenticated UI socket can settle it.
                from browser.pairing import pairing_broker
                pairing_broker.resolve(str(msg.get("request_id", "")), bool(msg.get("approve", False)))
            elif t == "plan_decision":      # Proceed / Cancel buttons of the agent's plan preview
                await self.start_task("proceed" if msg.get("approve") else "cancel", "text")
            elif t == "replay_workflow":
                await self.replay_workflow(str(msg.get("id", "")))
            elif t == "new_chat":
                self.reset_agent()
                await self.broadcast({"type": "chat_reset"}, persist=False)
            elif t == "ping":
                await client.send_json({"type": "pong", "ts": time.time()})
        except BusyError as e:
            await client.send_json({"type": "error", "message": str(e), "hint": "Press Stop to cancel the running task."})
        except ValueError as e:
            await client.send_json({"type": "error", "message": str(e), "hint": ""})
