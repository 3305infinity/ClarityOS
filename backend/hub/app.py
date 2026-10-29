"""
Moonwalk Hub — HTTP / WebSocket server (FastAPI)

    GET  /                      the website (static, no secrets in it)
    /api/*                      REST, requires  Authorization: Bearer <ui token>
    WS   /ws                    website  <-> hub   (first message must be {"type":"auth","token":...})
    WS   /helper                Windows helper <-> hub (first message {"type":"hello","token":<helper token>,...})
    WS   127.0.0.1:8765         Chrome extension bridge (token + Origin chrome-extension:// only)

Everything binds to 127.0.0.1. Requests are rejected unless the Host header is our own loopback
name (DNS-rebinding defence) and, for browsers, the Origin is our own site (or our extension).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from hub import config as cfg
from hub.core import Client, Hub
from hub.security import bearer_from_header, constant_eq, host_allowed, origin_allowed

DEFAULT_PORT = int(os.environ.get("MOONWALK_HUB_PORT", "8787"))
AUTH_TIMEOUT_S = 5.0

GNANI_VOICES = {   # Timbre v2.5 (one preferred language each) — from Gnani's voice catalog
    "hi-en (Hinglish)": ["Poorvi"],
    "hi-IN (Hindi)": ["Nalini", "Bhavna", "Yashvi", "Urmila", "Jwala", "Chitra", "Ambuja", "Deepak", "Roopesh", "Vikrant", "Hemraj", "Jalaj", "Omkar"],
    "en-IN (English)": ["Kaveri", "Trupti", "Devika", "Pranav", "Shlok", "Girish"],
    "ta-IN": ["Asmita", "Trisha", "Brinda", "Vedika", "Noopur"], "te-IN": ["Suhana", "Lehara", "Lavanya", "Yukti", "Varuni"],
    "kn-IN": ["Saanvi", "Kavin"], "ml-IN": ["Reshma", "Riyaan"], "mr-IN": ["Zahira", "Ishaan"],
    "bn-IN": ["Kirra", "Dhruva"], "gu-IN": ["Falak", "Veera"], "pa-IN": ["Mehuli", "Zayan"],
}


def _web_dir() -> Path:
    env = os.environ.get("MOONWALK_WEB_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "web_app"


class _WSJson:
    """Adapts a FastAPI WebSocket to the `send_json` protocol used by Hub / HelperLink."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._lock = asyncio.Lock()

    async def send_json(self, data: Dict[str, Any]) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False, default=str))


# ═══════════════════════════════════════════════════════════════
#  Extension bridge (websockets server) with Origin enforcement
# ═══════════════════════════════════════════════════════════════

async def start_bridge(hub: Hub, host: str = "127.0.0.1", port: int = 8765):
    from websockets.asyncio.server import serve
    from browser import browser_bridge
    from servers.browser_bridge_server import bridge_handler

    browser_bridge._session_token = hub.tokens["bridge"]   # random pairing code instead of "dev-bridge-token"

    # Let an unpaired extension ask to be paired. The broker blocks until the
    # user clicks Approve in this hub's UI; it never issues the code by itself.
    from browser.pairing import pairing_broker

    async def _ask_to_pair(event: dict) -> None:
        await hub.broadcast({"type": "bridge_pair_request", **event}, persist=False)

    pairing_broker.configure(_ask_to_pair, lambda: hub.tokens["bridge"])

    def process_request(connection, request):
        origin = request.headers.get("Origin")
        if not origin_allowed(origin, 0, allow_extension=True, allow_missing=True):
            return connection.respond(HTTPStatus.FORBIDDEN, "Origin not allowed\n")
        return None

    server = await serve(bridge_handler, host, port, process_request=process_request,
                         ping_interval=30, ping_timeout=60, max_size=8 * 1024 * 1024)
    print(f"[Hub] Extension bridge on ws://{host}:{port} (extension origin only)")
    return server


# ═══════════════════════════════════════════════════════════════
#  App factory
# ═══════════════════════════════════════════════════════════════

def create_app(hub: Optional[Hub] = None, port: int = DEFAULT_PORT, web_dir: Optional[Path] = None,
               start_bridge_server: bool = True, install_tools: bool = True, bridge_port: int = 8765) -> FastAPI:
    web = Path(web_dir) if web_dir else _web_dir()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal hub
        if hub is None:
            hub = Hub()
        app.state.hub = hub
        if install_tools:
            from agent import create_agent  # noqa: F401  (import order: agent before tools avoids a circular import)
            import tools
            from hub import desktop_proxy
            desktop_proxy.install(hub.helper, tools.registry)
            hub.install_guard(tools.registry)
        bridge = None
        if start_bridge_server:
            try:
                bridge = await start_bridge(hub, port=bridge_port)
            except OSError as e:
                print(f"[Hub] ⚠ extension bridge not started: {e}")

        async def poll():
            while True:
                await asyncio.sleep(3)
                try:
                    await hub.broadcast_connections()
                except Exception:
                    pass

        poller = asyncio.create_task(poll())
        yield
        poller.cancel()
        await hub.cancel("shutdown")
        if bridge:
            bridge.close()
        hub.store.close()

    app = FastAPI(title="Moonwalk Hub", version=cfg.VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if hub is not None:
        app.state.hub = hub

    def H() -> Hub:
        return app.state.hub

    # ── security middleware ──
    csp = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           f"connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; media-src 'self' blob: data:; "
           "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

    @app.middleware("http")
    async def security(request: Request, call_next):
        if not host_allowed(request.headers.get("host"), port):
            return JSONResponse({"error": "bad host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not origin_allowed(origin, port):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        if request.url.path.startswith("/api/"):
            tok = bearer_from_header(request.headers.get("authorization"))
            if not constant_eq(tok, H().tokens["ui"]):
                await asyncio.sleep(0.3)  # slow down guessing
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = csp
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        return resp

    # ── static site ──
    @app.get("/")
    async def index():
        f = web / "index.html"
        if not f.exists():
            raise HTTPException(404, "web_app/index.html not found")
        return FileResponse(f, media_type="text/html")

    if web.exists():
        app.mount("/static", StaticFiles(directory=str(web)), name="static")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": cfg.VERSION}

    # ── REST ──
    def state_payload() -> Dict[str, Any]:
        h = H()
        return {
            "version": cfg.VERSION,
            "connections": h.connections(),
            "settings": h.settings.get(),
            "secrets": h.secrets.configured(),
            "secret_labels": cfg.SECRET_NAMES,
            "capabilities": cfg.CAPABILITIES,
            "options": {"gnani_voices": GNANI_VOICES,
                        "stt_languages": ["en-IN", "hi-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "bn-IN", "gu-IN", "pa-IN"]},
        }

    @app.get("/api/state")
    async def api_state():
        return state_payload()

    @app.put("/api/settings")
    async def api_settings(request: Request):
        h = H()
        try:
            new = h.settings.update(await request.json())
        except (cfg.SettingsError, ValueError) as e:
            raise HTTPException(400, str(e))
        cfg.apply_settings_to_env(new)
        await h.broadcast_connections(force=True)
        return state_payload()

    @app.put("/api/secrets/{name}")
    async def api_secret_set(name: str, request: Request):
        h = H()
        body = await request.json()
        try:
            h.secrets.set(name, str(body.get("value", "")))
        except cfg.SettingsError as e:
            raise HTTPException(400, str(e))
        if name == "GEMINI_API_KEY":
            h.reset_agent()
        from voice import stt, tts
        stt.reset_stt_provider()
        tts.reset_tts_engine()
        await h.broadcast_connections(force=True)
        return {"ok": True, "configured": h.secrets.configured(), "persistent": h.secrets.persistent}

    @app.delete("/api/secrets/{name}")
    async def api_secret_delete(name: str):
        h = H()
        if name not in cfg.SECRET_NAMES:
            raise HTTPException(404, "unknown secret")
        h.secrets.delete(name)
        h.reset_agent()
        await h.broadcast_connections(force=True)
        return {"ok": True, "configured": h.secrets.configured()}

    # ── Browser bridge owner surface ──
    # Served by whichever process owns port 8765. The other ClarityOS process
    # borrows these instead of opening a competing listener. Auth is the same
    # /api/ bearer token used everywhere else.
    @app.get("/api/browser/status")
    async def api_browser_status():
        from browser import owner_api
        return owner_api.status()

    @app.get("/api/browser/snapshot")
    async def api_browser_snapshot(session_id: str = "", timeout: float = 8.0):
        from browser import owner_api
        return await owner_api.snapshot(session_id=session_id, timeout=timeout)

    @app.post("/api/browser/action")
    async def api_browser_action(request: Request):
        from browser import owner_api
        body = await request.json()
        return await owner_api.action(body.get("action") or {},
                                      timeout=float(body.get("timeout", 20.0) or 20.0))

    @app.get("/api/browser/page")
    async def api_browser_page(session_id: str = "", timeout: float = 10.0):
        from browser import owner_api
        return await owner_api.page(session_id=session_id, timeout=timeout)

    @app.get("/api/pairing")
    async def api_pairing():
        h = H()
        return {"bridge_token": h.tokens["bridge"], "helper_token": h.tokens["helper"],
                "bridge_url": "ws://127.0.0.1:8765", "helper_url": f"ws://127.0.0.1:{port}/helper"}

    @app.post("/api/pairing/rotate/{name}")
    async def api_rotate(name: str):
        h = H()
        if name not in ("bridge", "helper"):
            raise HTTPException(404, "unknown token")
        h.tokens[name] = cfg.rotate_token(name)
        if name == "bridge":
            from browser import browser_bridge
            browser_bridge._session_token = h.tokens["bridge"]
            browser_bridge.disconnect()
        return {"ok": True, "token": h.tokens[name]}

    @app.get("/api/tasks")
    async def api_tasks(limit: int = 100):
        return {"tasks": H().store.list_tasks(max(1, min(limit, 500)))}

    @app.get("/api/tasks/{tid}")
    async def api_task(tid: str):
        t = H().store.get_task(tid)
        if not t:
            raise HTTPException(404, "task not found")
        return t

    @app.delete("/api/tasks/{tid}")
    async def api_task_delete(tid: str):
        return {"ok": H().store.delete_task(tid)}

    @app.delete("/api/tasks")
    async def api_tasks_clear():
        H().store.clear_history()
        return {"ok": True}

    @app.get("/api/export")
    async def api_export():
        data = H().store.export_all()
        return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="moonwalk-task-log.json"'})

    @app.get("/api/workflows")
    async def api_workflows():
        return {"workflows": H().store.list_workflows()}

    @app.post("/api/workflows")
    async def api_workflow_save(request: Request):
        body = await request.json()
        try:
            wid = H().store.save_workflow(str(body.get("name", "")), list(body.get("steps") or []))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": wid}

    @app.delete("/api/workflows/{wid}")
    async def api_workflow_delete(wid: str):
        return {"ok": H().store.delete_workflow(wid)}

    # ── website WebSocket ──
    @app.websocket("/ws")
    async def ws_site(ws: WebSocket):
        h = H()
        if not host_allowed(ws.headers.get("host"), port) or not origin_allowed(ws.headers.get("origin"), port):
            await ws.close(code=4403)
            return
        await ws.accept()
        sock = _WSJson(ws)
        client = Client(sock.send_json)
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S))
            if first.get("type") != "auth" or not constant_eq(str(first.get("token", "")), h.tokens["ui"]):
                await asyncio.sleep(0.3)
                await ws.close(code=4401)
                return
        except Exception:
            await ws.close(code=4401)
            return
        client.authenticated = True
        h.clients.add(client)
        await sock.send_json({"type": "hello", "version": cfg.VERSION, "state": state_payload(), "ts": time.time()})
        try:
            while True:
                raw = await ws.receive_text()
                if len(raw) > 8 * 1024 * 1024:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await h.handle_client_message(client, msg)
        except WebSocketDisconnect:
            pass
        finally:
            h.clients.discard(client)

    # ── Windows helper WebSocket ──
    @app.websocket("/helper")
    async def ws_helper(ws: WebSocket):
        h = H()
        if not host_allowed(ws.headers.get("host"), port) or ws.headers.get("origin"):
            await ws.close(code=4403)    # the helper is not a browser: it never sends Origin
            return
        await ws.accept()
        sock = _WSJson(ws)
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S))
            if hello.get("type") != "hello" or not constant_eq(str(hello.get("token", "")), h.tokens["helper"]):
                await sock.send_json({"type": "hello_ack", "ok": False, "message": "invalid token"})
                await asyncio.sleep(0.3)
                await ws.close(code=4401)
                return
        except Exception:
            await ws.close(code=4401)
            return
        await h.helper.attach(sock, hello)
        await sock.send_json({"type": "hello_ack", "ok": True, "message": "connected"})
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if isinstance(msg, dict):
                    if msg.get("type") == "ping":
                        await sock.send_json({"type": "pong"})
                    else:
                        await h.helper.handle_message(msg)
        except (WebSocketDisconnect, ValueError):
            pass
        finally:
            await h.helper.detach(sock)

    return app
