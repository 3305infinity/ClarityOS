"""End-to-end hub tests: website WS + helper WS + policy/approval + cancellation (no network, fake agent)."""
import base64
import io
import json
import math
import struct
import threading
import wave

import pytest
from fastapi.testclient import TestClient

from agent import create_agent  # noqa: F401  (import order: agent before tools)
import tools
from hub.app import create_app
from hub.core import Hub

PORT = 8787
HOST = {"host": f"127.0.0.1:{PORT}"}


class FakeAgent:
    """Stands in for the Gemini-driven SPAV agent: runs a scripted list of tool calls."""
    script = []

    async def run(self, text, context, ws_callback=None):
        out = []
        for name, args in type(self).script:
            out.append(await tools.registry.execute(name, args))
        if ws_callback:
            await ws_callback({"type": "doing", "text": "working", "tool": "fake"})
            await ws_callback({"type": "response", "payload": {"text": "done: " + text}})
        return ("done: " + text, False)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import agent.perception as perception

    async def fake_snapshot(text="", include_vision=False):
        return object()
    monkeypatch.setattr(perception, "snapshot", fake_snapshot)
    hub = Hub(data_dir=tmp_path, agent_factory=FakeAgent, use_keyring=False)
    hub.settings.update({"tts_enabled": False})
    app = create_app(hub=hub, port=PORT, web_dir=tmp_path, start_bridge_server=False, install_tools=True)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        yield hub, client
        for h in OPEN_HELPERS:
            h.close()
        OPEN_HELPERS.clear()


def auth(hub):
    return {"Authorization": "Bearer " + hub.tokens["ui"]}


def site_ws(client, hub):
    ws = client.websocket_connect("/ws", headers={**HOST, "origin": f"http://127.0.0.1:{PORT}"})
    s = ws.__enter__()
    s.send_json({"type": "auth", "token": hub.tokens["ui"]})
    hello = s.receive_json()
    assert hello["type"] == "hello"
    return ws, s


def until(sock, wanted, limit=60):
    seen = []
    for _ in range(limit):
        m = sock.receive_json()
        seen.append(m["type"])
        if m["type"] == wanted:
            return m, seen
    raise AssertionError(f"never saw {wanted}; saw {seen}")


OPEN_HELPERS = []


class HelperThread:
    """Fake Windows helper: answers every call; records them."""
    def __init__(self, client, hub, responses=None):
        self.calls = []
        self.cancelled = threading.Event()
        self.responses = responses or {}
        self.ws = client.websocket_connect("/helper", headers=HOST)
        self.s = self.ws.__enter__()
        self.s.send_json({"type": "hello", "token": hub.tokens["helper"], "version": "t", "host": "T", "os": "Win", "elevated": False,
                          "tools": ["run_shell", "click_element", "get_active_app", "get_window_title", "get_clipboard", "capture_screen"]})
        assert self.s.receive_json()["ok"] is True
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()
        OPEN_HELPERS.append(self)

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass
        try:
            self.ws.__exit__(None, None, None)
        except Exception:
            pass

    def loop(self):
        try:
            while True:
                m = self.s.receive_json()
                if m["type"] == "call":
                    self.calls.append((m["tool"], m["args"]))
                    res = self.responses.get(m["tool"], "ok:" + m["tool"])
                    self.s.send_json({"type": "result", "id": m["id"], "ok": True, "result": res})
                elif m["type"] == "cancel":
                    self.cancelled.set()
        except Exception:
            pass


# ─────────────── security ───────────────

def test_api_requires_token(env):
    hub, c = env
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/state", headers=auth(hub)).status_code == 200


def test_bad_host_and_origin_rejected(env):
    hub, c = env
    assert c.get("/healthz", headers={"host": "evil.example.com"}).status_code == 403
    assert c.get("/api/state", headers={**auth(hub), "origin": "https://evil.example.com"}).status_code == 403
    assert c.get("/healthz").status_code == 200


def test_ws_origin_and_auth(env):
    hub, c = env
    # a web page on another origin must be refused outright
    with pytest.raises(Exception):
        with c.websocket_connect("/ws", headers={**HOST, "origin": "https://evil.example.com"}) as s:
            s.receive_json()
    # right origin but wrong token
    with c.websocket_connect("/ws", headers={**HOST, "origin": f"http://127.0.0.1:{PORT}"}) as s:
        s.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(Exception):
            s.receive_json()
    # helper endpoint refuses browsers (anything with an Origin header) and bad tokens
    with pytest.raises(Exception):
        with c.websocket_connect("/helper", headers={**HOST, "origin": "https://evil.example.com"}) as s:
            s.receive_json()
    with c.websocket_connect("/helper", headers=HOST) as s:
        s.send_json({"type": "hello", "token": "bad"})
        assert s.receive_json()["ok"] is False


def test_security_headers(env):
    hub, c = env
    r = c.get("/healthz")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


# ─────────────── settings & secrets ───────────────

def test_settings_validation_and_secrets_never_returned(env):
    hub, c = env
    assert c.put("/api/settings", json={"stt_provider": "nonsense"}, headers=auth(hub)).status_code == 400
    assert c.put("/api/settings", json={"tts_speed": 9}, headers=auth(hub)).status_code == 400
    assert c.put("/api/settings", json={"permissions": {"shell": "yolo"}}, headers=auth(hub)).status_code == 400
    r = c.put("/api/settings", json={"stt_provider": "gnani", "stt_language": "en-IN", "tts_speed": 1.2}, headers=auth(hub))
    assert r.status_code == 200 and r.json()["settings"]["stt_provider"] == "gnani"
    secret = "gnani-SECRET-VALUE-1234567890"
    r = c.put("/api/secrets/GNANI_API_KEY", json={"value": secret}, headers=auth(hub))
    assert r.status_code == 200
    state = c.get("/api/state", headers=auth(hub))
    assert state.json()["secrets"]["GNANI_API_KEY"] is True
    assert secret not in state.text and secret not in c.get("/api/pairing", headers=auth(hub)).text
    assert c.put("/api/secrets/NOT_A_KEY", json={"value": "x" * 20}, headers=auth(hub)).status_code == 400


# ─────────────── policy / approval / helper proxy ───────────────

def test_shell_needs_approval_then_runs_on_helper(env):
    hub, c = env
    FakeAgent.script = [("run_shell", {"command": "echo hi", "reasoning": "test"})]
    ws, s = site_ws(c, hub)
    helper = HelperThread(c, hub)
    until(s, "connections")
    s.send_json({"type": "command", "text": "run echo"})
    req, _ = until(s, "approval_request")
    assert req["capability"] == "shell" and "run_shell" in req["summary"]
    s.send_json({"type": "approval_response", "approval_id": req["approval_id"], "approve": True})
    res, _ = until(s, "tool_result")
    assert res["ok"] is True and res["preview"] == "ok:run_shell"
    fin, _ = until(s, "task_finished")
    assert fin["status"] == "done"
    assert ("run_shell", {"command": "echo hi"}) in helper.calls
    ws.__exit__(None, None, None)


def test_denied_approval_blocks_tool(env):
    hub, c = env
    FakeAgent.script = [("run_shell", {"command": "echo hi", "reasoning": "t"})]
    ws, s = site_ws(c, hub)
    helper = HelperThread(c, hub)
    s.send_json({"type": "command", "text": "x"})
    req, _ = until(s, "approval_request")
    s.send_json({"type": "approval_response", "approval_id": req["approval_id"], "approve": False})
    res, _ = until(s, "tool_result")
    assert res["ok"] is False and "policy.user_denied" in res["preview"]
    until(s, "task_finished")
    assert not any(t == "run_shell" for t, _ in helper.calls)
    ws.__exit__(None, None, None)


def test_deny_mode_blocks_without_asking(env):
    hub, c = env
    hub.settings.update({"permissions": {"desktop": "deny"}})
    FakeAgent.script = [("click_element", {"x": 5, "y": 6, "reasoning": "t"})]
    ws, s = site_ws(c, hub)
    HelperThread(c, hub)
    s.send_json({"type": "command", "text": "x"})
    blocked, seen = until(s, "tool_blocked")
    assert "approval_request" not in seen and blocked["capability"] == "desktop"
    ws.__exit__(None, None, None)


def test_click_on_risky_label_asks_but_safe_click_does_not(env):
    hub, c = env
    FakeAgent.script = [("click_element", {"x": 1, "y": 2, "reasoning": "click the Pay now button"})]
    ws, s = site_ws(c, hub)
    helper = HelperThread(c, hub)
    s.send_json({"type": "command", "text": "x"})
    req, _ = until(s, "approval_request")
    assert "pay" in req["reason"].lower()
    s.send_json({"type": "approval_response", "approval_id": req["approval_id"], "approve": True})
    until(s, "task_finished")
    # positional-arg proxy: helper got named args incl. defaults
    assert helper.calls[-1][0] == "click_element" and helper.calls[-1][1]["x"] == 1

    FakeAgent.script = [("click_element", {"x": 3, "y": 4, "reasoning": "click the Next link"})]
    s.send_json({"type": "command", "text": "y"})
    _, seen = until(s, "task_finished")
    assert "approval_request" not in seen
    ws.__exit__(None, None, None)


def test_desktop_tools_fail_cleanly_without_helper(env):
    hub, c = env
    FakeAgent.script = [("click_element", {"x": 1, "y": 2, "reasoning": "go"})]
    ws, s = site_ws(c, hub)
    s.send_json({"type": "command", "text": "x"})
    res, _ = until(s, "tool_result")
    assert res["ok"] is False and "helper is not connected" in res["preview"]
    ws.__exit__(None, None, None)


def test_cancel_propagates_to_helper_and_task(env):
    hub, c = env
    import asyncio

    class SlowAgent:
        async def run(self, text, context, ws_callback=None):
            await asyncio.sleep(30)
            return ("never", False)
    hub._agent_factory = SlowAgent
    hub.agent = None
    ws, s = site_ws(c, hub)
    helper = HelperThread(c, hub)
    s.send_json({"type": "command", "text": "slow"})
    until(s, "task_started")
    s.send_json({"type": "cancel"})
    fin, _ = until(s, "task_finished")
    assert fin["status"] == "cancelled"
    assert helper.cancelled.wait(3), "helper never received the cancel"
    ws.__exit__(None, None, None)


def test_second_command_while_busy_is_rejected(env):
    hub, c = env
    import asyncio

    class SlowAgent:
        async def run(self, text, context, ws_callback=None):
            await asyncio.sleep(30)
            return ("x", False)
    hub._agent_factory = SlowAgent
    ws, s = site_ws(c, hub)
    s.send_json({"type": "command", "text": "one"})
    until(s, "task_started")
    s.send_json({"type": "command", "text": "two"})
    err, _ = until(s, "error")
    assert "already running" in err["message"]
    s.send_json({"type": "cancel"})
    until(s, "task_finished")
    ws.__exit__(None, None, None)


def test_pause_blocks_next_tool_until_resume(env):
    hub, c = env
    FakeAgent.script = [("get_running_apps", {"reasoning": "t"})]
    ws, s = site_ws(c, hub)
    HelperThread(c, hub)
    s.send_json({"type": "pause"})
    until(s, "paused")
    s.send_json({"type": "command", "text": "x"})
    until(s, "paused")                      # guard announces it is waiting
    s.send_json({"type": "resume"})
    fin, seen = until(s, "task_finished")
    assert "tool_result" in seen and fin["status"] == "done"
    ws.__exit__(None, None, None)


# ─────────────── history, redaction, export, workflows ───────────────

def test_history_is_redacted_and_exportable(env):
    hub, c = env
    secret = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE12"
    hub.secrets.set("GNANI_API_KEY", "gnani-secret-value-98765")
    FakeAgent.script = []
    ws, s = site_ws(c, hub)
    s.send_json({"type": "command", "text": f"remember my key {secret} and gnani-secret-value-98765 password=hunter2"})
    until(s, "task_finished")
    export = c.get("/api/export", headers=auth(hub))
    assert export.status_code == 200
    body = export.text
    assert secret not in body and "gnani-secret-value-98765" not in body and "hunter2" not in body
    assert "[REDACTED]" in body
    tasks = c.get("/api/tasks", headers=auth(hub)).json()["tasks"]
    assert tasks and tasks[0]["status"] == "done"
    detail = c.get(f"/api/tasks/{tasks[0]['id']}", headers=auth(hub)).json()
    assert {"task_started", "response", "task_finished"} <= {e["type"] for e in detail["events"]}
    assert c.delete("/api/tasks", headers=auth(hub)).json()["ok"]
    assert c.get("/api/tasks", headers=auth(hub)).json()["tasks"] == []
    ws.__exit__(None, None, None)


def test_workflow_save_and_replay(env):
    hub, c = env
    FakeAgent.script = []
    r = c.post("/api/workflows", json={"name": "morning", "steps": ["open mail", "read headlines"]}, headers=auth(hub))
    wid = r.json()["id"]
    assert c.post("/api/workflows", json={"name": "", "steps": []}, headers=auth(hub)).status_code == 400
    ws, s = site_ws(c, hub)
    s.send_json({"type": "replay_workflow", "id": wid})
    done, seen = until(s, "workflow_done", limit=80)
    assert seen.count("task_finished") == 2
    ws.__exit__(None, None, None)


# ─────────────── voice ───────────────

def wav_b64(seconds=1.0, amp=6000):
    n = int(16000 * seconds)
    frames = b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 220 * i / 16000))) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(frames)
    return base64.b64encode(buf.getvalue()).decode()


def test_voice_utterance_to_task_and_error_paths(env, monkeypatch):
    hub, c = env
    from voice import stt

    class FakeSTT:
        name = "fake"
        mode = "ok"
        async def transcribe(self, pcm, sample_rate=16000):
            if FakeSTT.mode == "none":
                raise stt.NoSpeechError("x")
            if FakeSTT.mode == "down":
                raise stt.STTRequestError("Gnani STT HTTP 403: credits")
            return "Chrome kholo aur GitHub check karo"
    monkeypatch.setattr(stt, "get_stt_provider", lambda: FakeSTT())
    FakeAgent.script = []
    ws, s = site_ws(c, hub)

    s.send_json({"type": "voice_utterance", "audio": wav_b64(1.0, amp=10)})       # near-silence: never calls STT
    e, _ = until(s, "transcript_error"); assert e["kind"] == "no_speech"

    FakeSTT.mode = "none"
    s.send_json({"type": "voice_utterance", "audio": wav_b64()})
    e, _ = until(s, "transcript_error"); assert e["kind"] == "no_speech"

    FakeSTT.mode = "down"
    s.send_json({"type": "voice_utterance", "audio": wav_b64()})
    e, _ = until(s, "transcript_error"); assert e["kind"] == "stt_unavailable" and "type" in e["message"].lower()

    s.send_json({"type": "voice_utterance", "audio": "!!!not-base64"})
    e, _ = until(s, "transcript_error"); assert e["kind"] == "bad_audio"

    FakeSTT.mode = "ok"
    s.send_json({"type": "voice_utterance", "audio": wav_b64()})
    t, _ = until(s, "transcript")
    assert t["text"] == "Chrome kholo aur GitHub check karo"
    started, _ = until(s, "task_started"); assert started["source"] == "voice"
    until(s, "task_finished")
    ws.__exit__(None, None, None)


def test_website_is_served_with_csp_and_no_inline_script(tmp_path):
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parents[2] / "web_app"
    hub = Hub(data_dir=tmp_path, agent_factory=FakeAgent, use_keyring=False)
    app = create_app(hub=hub, port=PORT, web_dir=web, start_bridge_server=False, install_tools=False)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        html = c.get("/")
        assert html.status_code == 200 and "ClarityOS" in html.text   # product renamed from Moonwalk
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html.text), "inline <script> would be blocked by the CSP"
        js = c.get("/static/app.js").text
        assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write|eval\(", js)   # server text only via textContent
        for f in ("app.js", "styles.css", "worklet.js"):
            assert c.get("/static/" + f).status_code == 200
        assert "script-src 'self'" in html.headers["content-security-policy"]
