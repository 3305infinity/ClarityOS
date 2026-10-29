"""
Moonwalk — Cloud Orchestrator Server
======================================
WebSocket server that runs the Moonwalk agent brain in Google Cloud.

Architecture:
  Mac Client (mac_client.py) ←WebSocket→ Cloud Server (this file)

Protocol (incoming from Mac Client):
  - auth           { method, token, user_id }  — authenticate connection
  - transcription  { text, context }  — user speech / text
  - user_action    { action, text, context } — approve/cancel plan
  - tool_response  { call_id, result } — macOS tool execution result

Protocol (outgoing to Mac Client):
  - auth_result  { ok, user_id, error } — auth result
  - status    { state }        — state-idle, state-listening, state-loading
  - thinking  {}               — agent is reasoning
  - doing     { text, variant} — agent is executing a step
  - thought   { text }         — visible thought bubble
  - response  { payload }      — final response card
  - progress  { state }        — loading progress
  - tool_request { call_id, tool_name, tool_args } — execute this on Mac
  - await_reply  {}            — keep mic open for follow-up

Deployment:
  Cloud Run with PORT env var (default 8080).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
import time
import uuid
from functools import partial
from typing import Optional

import websockets  # type: ignore[import]
from dotenv import load_dotenv

# Force flush
print = partial(print, flush=True)

# Ensure package root is on path
_server_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_server_dir, ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# Load environment
load_dotenv(os.path.join(_backend_dir, ".env"))

# Mark as cloud environment before importing anything else
os.environ["MOONWALK_CLOUD"] = "1"

# Moonwalk modules
from agent.core_v2 import MoonwalkAgentV2
from agent.cloud_memory import (
    CloudConversationMemory,
    CloudUserProfile,
    CloudUserPreferences,
    CloudVaultMemory,
    CloudTaskStore,
    is_cloud,
)
from agent.rag import get_rag_engine, get_embed_fn
from agent.memory import WorkingMemory
from auth import verify_connection, AuthResult
from runtime_state import get_runtime_state, evict_runtime_state
from providers.router import ModelRouter
import agent.perception as perception
from tools import registry as tool_registry


# ═══════════════════════════════════════════════════════════════
#  Contextvars — per-request tool executor (replaces monkey-patch)
# ═══════════════════════════════════════════════════════════════

_tool_executor_var: contextvars.ContextVar[Optional[callable]] = contextvars.ContextVar(
    "tool_executor", default=None
)


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

PORT = int(os.environ.get("PORT", 8080))
HOST = os.environ.get("HOST", "0.0.0.0")

# Idle agent eviction (seconds)
AGENT_IDLE_TIMEOUT = int(os.environ.get("MOONWALK_AGENT_IDLE_TIMEOUT", 3600))

# Tools that MUST execute on the Mac (not in cloud)
MAC_ONLY_TOOLS = {
    # macOS GUI tools
    "open_app", "quit_app", "close_window",
    "click_ui", "type_in_field", "type_text",
    "press_key", "set_volume", "toggle_media",
    "get_ui_tree", "read_screen",
    "take_screenshot", "run_shortcut",
    "run_applescript", "get_clipboard", "set_clipboard",
    "move_mouse", "scroll_mouse",
    "open_file", "show_notification",
    # Spotlight / Finder
    "spotlight_search", "open_system_preferences",
}

# Tools safe to run in the cloud
CLOUD_SAFE_TOOLS = {
    "web_search", "fetch_web_content", "open_url",
    "send_response", "store_memory", "recall_memory",
    "get_current_time", "calculate",
    # Browser bridge tools (proxied through the Mac Client's bridge)
    "browser_snapshot", "browser_read_page", "browser_read_text",
    "browser_click_ref", "browser_type_ref", "browser_select_ref",
    "browser_scroll", "browser_find", "browser_click_match",
    "browser_refresh_refs", "browser_wait_for",
    "browser_list_tabs", "browser_switch_tab",
    # Google Workspace
    "gdocs_create", "gdocs_append", "gdocs_read", "gworkspace_analyze",
}


# ═══════════════════════════════════════════════════════════════
#  Cloud Agent — Wraps core_v2 with remote tool execution
# ═══════════════════════════════════════════════════════════════

class CloudAgent:
    """
    The cloud-hosted agent. Wraps MoonwalkAgentV2 and intercepts tool
    calls that need to run on the user's Mac, forwarding them via
    WebSocket and waiting for the result.
    """

    def __init__(self, user_id: str = "default"):
        self.user_id = user_id

        # Initialize RAG engine + embedding function
        self.rag = get_rag_engine()
        embed_fn = get_embed_fn()

        # Cloud-persistent memory
        self.conversation = CloudConversationMemory(user_id=user_id)
        self.profile = CloudUserProfile(user_id=user_id)
        self.preferences = CloudUserPreferences(user_id=user_id)
        self.vault = CloudVaultMemory(user_id=user_id, embed_fn=embed_fn)
        self.tasks = CloudTaskStore(user_id=user_id)
        self.working_memory = WorkingMemory(max_actions=40, max_entities=60)

        # Create the core agent
        self.agent = MoonwalkAgentV2(use_planning=True, persist=False)

        # Swap in cloud memory
        self.agent.conversation = self.conversation
        self.agent.user_profile = self.profile
        self.agent.preferences = self.preferences
        self.agent.vault = self.vault
        self.agent.task_store = self.tasks
        self.agent.working_memory = self.working_memory

        print(f"[CloudAgent] Initialized for user '{user_id}' with cloud memory + RAG")

    def _augment_with_rag(self, query: str) -> str:
        """Retrieve relevant vault knowledge for the current query."""
        try:
            results = self.vault.recall(query=query, max_results=5)
            return self.rag.augment_prompt(query, results)
        except Exception as e:
            print(f"[CloudAgent] RAG augmentation failed: {e}")
            return ""


# ═══════════════════════════════════════════════════════════════
#  Connection Handler — one per Mac Client
# ═══════════════════════════════════════════════════════════════

class ClientConnection:
    """
    Manages a single Mac Client connection.
    Handles the message loop, tool dispatch (cloud vs remote),
    and agent lifecycle.
    """

    def __init__(self, websocket, cloud_agent: CloudAgent):
        self.ws = websocket
        self.agent = cloud_agent
        self._pending_tool_calls: dict[str, asyncio.Future] = {}
        self._connected = True

    async def send(self, data: dict):
        """Send a JSON message to the Mac Client."""
        if self._connected:
            try:
                await self.ws.send(json.dumps(data))
            except Exception:
                self._connected = False

    async def ws_callback(self, payload: dict):
        """
        WebSocket callback passed to the agent.run() method.
        Forwards UI updates (thinking, doing, response, etc.) to the Mac Client.
        """
        await self.send(payload)

    async def request_mac_tool(self, tool_name: str, tool_args: dict) -> str:
        """
        Request the Mac Client to execute a macOS-specific tool.
        Sends a tool_request and waits for the tool_response.
        """
        call_id = uuid.uuid4().hex[:10]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_tool_calls[call_id] = future

        await self.send({
            "type": "tool_request",
            "call_id": call_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
        })

        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            return result
        except asyncio.TimeoutError:
            print(f"[Cloud] Tool request timed out: {tool_name}")
            return f"Error: Tool execution timed out after 120s"
        finally:
            self._pending_tool_calls.pop(call_id, None)

    def resolve_tool_response(self, call_id: str, result: str):
        """Resolve a pending tool request with its result."""
        future = self._pending_tool_calls.get(call_id)
        if future and not future.done():
            future.set_result(result)

    async def handle_transcription(self, data: dict):
        """Handle a transcribed user request from the Mac Client."""
        text = data.get("text", "").strip()
        context_data = data.get("context", {})

        if not text:
            return

        print(f"\n[Cloud] ═══ Request from '{self.agent.user_id}' ═══")
        print(f"[Cloud] Text: '{text}'")
        print(f"[Cloud] Context: app={context_data.get('active_app')}")

        # Build a ContextSnapshot from the Mac Client's context data
        context = perception.ContextSnapshot(
            active_app=context_data.get("active_app", ""),
            window_title=context_data.get("window_title", ""),
            browser_url=context_data.get("browser_url", ""),
            screen_text=context_data.get("screen_text", ""),
            screenshot_path=None,
        )

        # RAG augmentation — inject relevant vault knowledge
        rag_context = self.agent._augment_with_rag(text)
        if rag_context:
            print(f"[Cloud] RAG injected {len(rag_context)} chars of context")

        # Install a per-request tool interceptor via contextvars (concurrency-safe)
        original_execute = tool_registry.execute

        async def intercepted_execute(tool_name: str, args: dict):
            if tool_name in MAC_ONLY_TOOLS:
                print(f"[Cloud] → Forwarding to Mac: {tool_name}")
                return await self.request_mac_tool(tool_name, args)
            else:
                return await original_execute(tool_name, args)

        # Set the per-request executor in contextvars
        token = _tool_executor_var.set(intercepted_execute)

        try:
            response_text, awaiting = await self.agent.agent.run(
                user_text=text,
                context=context,
                ws_callback=self.ws_callback,
            )

            if awaiting:
                await self.send({"type": "await_reply"})

        except Exception as e:
            print(f"[Cloud] Agent error: {e}")
            import traceback
            traceback.print_exc()
            await self.send({
                "type": "response",
                "payload": {
                    "text": f"Sorry, I ran into an error: {str(e)[:200]}",
                    "display": "card",
                    "app": "",
                },
            })
        finally:
            _tool_executor_var.reset(token)

    async def handle_user_action(self, data: dict):
        """Handle user_action (approve/cancel plan) from Mac Client."""
        action = data.get("action", "")
        text = data.get("text", action)
        context_data = data.get("context", {})

        context = perception.ContextSnapshot(
            active_app=context_data.get("active_app", ""),
            window_title=context_data.get("window_title", ""),
            browser_url=context_data.get("browser_url", ""),
            screen_text=context_data.get("screen_text", ""),
            screenshot_path=None,
        )

        await self.handle_transcription({
            "text": text,
            "context": context_data,
        })

    async def run(self):
        """Main message loop for this client connection."""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")

                if msg_type == "transcription":
                    await self.handle_transcription(data)

                elif msg_type == "user_action":
                    await self.handle_user_action(data)

                elif msg_type == "tool_response":
                    call_id = data.get("call_id", "")
                    result = data.get("result", "")
                    self.resolve_tool_response(call_id, result)

                elif msg_type == "text_input":
                    text = data.get("text", "").strip()
                    if text:
                        await self.handle_transcription({
                            "text": text,
                            "context": data.get("context", {}),
                        })

                elif msg_type == "ping":
                    await self.send({"type": "pong"})

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[Cloud] Client disconnected: {e}")
        except Exception as e:
            print(f"[Cloud] Connection error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._connected = False
            print(f"[Cloud] Client '{self.agent.user_id}' disconnected")


# ═══════════════════════════════════════════════════════════════
#  Server — accepts Mac Client connections
# ═══════════════════════════════════════════════════════════════

# Per-user agents (persistent across reconnections)
_agents: dict[str, CloudAgent] = {}
_agent_last_seen: dict[str, float] = {}
_agents_lock = asyncio.Lock()


async def _get_or_create_agent(user_id: str) -> CloudAgent:
    """Get or create a CloudAgent for a user. Agents persist across reconnections."""
    async with _agents_lock:
        if user_id not in _agents:
            _agents[user_id] = CloudAgent(user_id=user_id)
        _agent_last_seen[user_id] = time.time()
        return _agents[user_id]


async def _evict_idle_agents():
    """Periodically evict agents that haven't been used recently."""
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        now = time.time()
        to_evict = []
        async with _agents_lock:
            for uid, last_seen in list(_agent_last_seen.items()):
                if now - last_seen > AGENT_IDLE_TIMEOUT:
                    to_evict.append(uid)
            for uid in to_evict:
                _agents.pop(uid, None)
                _agent_last_seen.pop(uid, None)
                evict_runtime_state(uid)
        if to_evict:
            print(f"[Cloud] Evicted {len(to_evict)} idle agent(s): {to_evict}")


async def handler(websocket):
    """WebSocket connection handler for incoming Mac Client connections."""
    remote = websocket.remote_address
    print(f"[Cloud] New connection from {remote}")

    # ── Authentication (required) ──
    try:
        first_msg = await asyncio.wait_for(websocket.recv(), timeout=15.0)
        auth_data = json.loads(first_msg)
    except asyncio.TimeoutError:
        await websocket.close(4001, "Auth timeout")
        print(f"[Cloud] Auth timeout from {remote}")
        return
    except (json.JSONDecodeError, Exception):
        await websocket.close(4001, "Invalid auth message")
        return

    auth_result = await verify_connection(auth_data)

    if not auth_result.ok:
        await websocket.send(json.dumps({
            "type": "auth_result",
            "ok": False,
            "error": auth_result.error,
        }))
        await websocket.close(4001, auth_result.error)
        print(f"[Cloud] Rejected connection from {remote}: {auth_result.error}")
        return

    user_id = auth_result.user_id
    await websocket.send(json.dumps({
        "type": "auth_result",
        "ok": True,
        "user_id": user_id,
        "email": auth_result.email,
        "name": auth_result.name,
        "method": auth_result.method,
    }))

    agent = await _get_or_create_agent(user_id)
    conn = ClientConnection(websocket, agent)

    print(f"[Cloud] Client authenticated: user='{user_id}' method={auth_result.method}")
    await conn.send({"type": "status", "state": "state-idle"})

    await conn.run()


# ═══════════════════════════════════════════════════════════════
#  Health Check (HTTP for Cloud Run / load balancers)
# ═══════════════════════════════════════════════════════════════

async def health_handler(path, request_headers):
    """
    HTTP health check endpoint for Cloud Run.
    Returns 200 for GET /health or GET /.
    WebSocket upgrades pass through normally.
    """
    if path in ("/health", "/healthz", "/"):
        if "upgrade" not in {k.lower(): v for k, v in request_headers.raw_items()}.get("upgrade", "").lower():
            return (200, [("Content-Type", "application/json")], json.dumps({
                "status": "healthy",
                "service": "moonwalk-cloud",
                "active_agents": len(_agents),
                "uptime_seconds": int(time.time() - _start_time),
            }).encode())
    return None


_start_time = time.time()


async def main():
    """Start the Cloud Orchestrator WebSocket server."""
    auth_mode = os.environ.get("MOONWALK_CLOUD_TOKEN", "")
    google_client_id = os.environ.get("MOONWALK_GOOGLE_CLIENT_ID", "")

    print("=" * 60)
    print("  Moonwalk Cloud Orchestrator")
    print("=" * 60)
    print(f"  Host:     {HOST}")
    print(f"  Port:     {PORT}")
    print(f"  Auth:     token={'yes' if auth_mode else 'no (dev mode)'}")
    print(f"  Google:   {'configured' if google_client_id else 'not configured'}")
    print(f"  Cloud:    {is_cloud()}")
    print("=" * 60)

    # Pre-initialize the model router to verify API keys
    try:
        router = ModelRouter()
        print(f"[Cloud] Model router ready")
    except Exception as e:
        print(f"[Cloud] WARNING: Router init failed: {e}")

    # Start idle agent eviction task
    asyncio.create_task(_evict_idle_agents())

    async with websockets.serve(
        handler,
        HOST,
        PORT,
        ping_interval=120,
        ping_timeout=600,
        max_size=10 * 1024 * 1024,  # 10 MB
        origins=None,
        process_request=health_handler,
    ):
        print(f"\n[Cloud] Listening on ws://{HOST}:{PORT}")
        print(f"[Cloud] Health check: http://{HOST}:{PORT}/health")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
