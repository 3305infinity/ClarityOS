"""
Moonwalk — Browser Bridge State
===============================
Tracks extension connection state and queued browser actions.
"""

import asyncio
import os
import urllib.parse
import secrets
import time
from typing import Dict, List, Optional
from .models import ActionRequest, ActionResult, DomChangeEvent, PageSnapshot
from .store import browser_store
from runtime_state import runtime_state_store


def _default_bridge_token() -> str:
    """
    The pairing code the extension must present.

    One source of truth, in priority order:

      1. MOONWALK_BROWSER_BRIDGE_TOKEN — explicit override
      2. tokens.json "bridge"          — the pairing code the hub shows in
                                         Settings → Connections

    Both backends serve this bridge on the same port: the hub
    (hub/app.py start_bridge) and the Electron voice server
    (servers/local_server.py). They previously disagreed — the hub used
    tokens.json while the Electron path fell back to a literal
    "dev-bridge-token" — so whichever process happened to own port 8765
    decided which pairing code was valid, and the other one produced
    "Invalid bridge token". Resolving tokens.json here makes both agree.
    """
    configured = os.environ.get("MOONWALK_BROWSER_BRIDGE_TOKEN", "").strip()
    if configured:
        return configured
    try:
        from hub.config import load_tokens          # creates tokens.json on first run
        token = (load_tokens().get("bridge") or "").strip()
        if token:
            return token
    except Exception as e:                           # keyring/FS unavailable — never fatal
        print(f"[BrowserBridge] could not read tokens.json ({e.__class__.__name__}); "
              f"set MOONWALK_BROWSER_BRIDGE_TOKEN to pair the extension")
    # Deliberately unguessable: an unpaired bridge must reject everything
    # rather than accept a well-known literal.
    return secrets.token_urlsafe(32)


async def read_page_via_owner(session_id: str = "", timeout: float = 8.0):
    """
    Ask the ClarityOS process that owns the extension bridge to read the tab.

    Only one process can bind port 8765. The other one has an empty
    BrowserBridge, so its browser tools could never reach Chrome. Instead of
    making the user close a window, borrow the owner's connection over the
    hub's existing authenticated API. Returns the same dict shape as a local
    read, or None when there is no owner to ask.
    """
    import json as _json
    import urllib.error
    import urllib.request
    from pathlib import Path

    hub_port = int(os.environ.get("MOONWALK_HUB_PORT", "8787"))
    try:
        from hub.config import load_tokens
        ui_token = (load_tokens().get("ui") or "").strip()
    except Exception:
        ui_token = ""
    if not ui_token:
        return None

    url = f"http://127.0.0.1:{hub_port}/api/browser/page"
    if session_id:
        url += f"?session_id={urllib.parse.quote(session_id)}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {ui_token}")
    req.add_header("Host", f"127.0.0.1:{hub_port}")

    def _fetch():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _fetch)
    except Exception:
        return None


def bridge_disconnect_reason() -> str:
    """
    Why is no extension attached to *this* process?

    The hub and the Electron voice server both serve the extension bridge on
    the same port, so only one of them actually owns it. The other holds a
    BrowserBridge with no extension, and every browser tool there fails. Saying
    "not connected" sent people to re-pair an extension that was already paired
    — to the other process. Name the real situation instead.
    """
    import socket
    port = int(os.environ.get("MOONWALK_BROWSER_BRIDGE_PORT", "8765"))
    probe = socket.socket()
    probe.settimeout(0.4)
    try:
        in_use = probe.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        in_use = False
    finally:
        probe.close()
    if in_use:
        return (f"The Chrome extension is connected to a different ClarityOS process "
                f"(something else owns port {port} — normally the ClarityOS hub). "
                f"Ask from the hub window at http://127.0.0.1:8787, or close the hub so "
                f"this app owns the bridge.")
    return ("The Chrome extension is not connected. Open the extension popup and "
            "click Pair this extension, then approve it in ClarityOS.")


class BrowserBridge:
    # If no heartbeat/snapshot is received within this window (seconds),
    # consider the extension disconnected even though it never sent a
    # disconnect event.
    STALE_THRESHOLD: float = 60.0

    def __init__(self):
        # Resolved lazily on first use: reading tokens.json at import time would
        # pull hub.config into the import graph (circular) and do file I/O
        # before the process has decided which backend it is.
        self._session_token: Optional[str] = None
        self._connected_session_id: Optional[str] = None
        self._last_seen_at: float = 0.0
        self._pending_actions: List[ActionRequest] = []
        self._extension_name: str = ""
        self._latest_result_by_action_id: Dict[str, ActionResult] = {}
        self._latest_dom_change_by_action_id: Dict[str, DomChangeEvent] = {}
        # Event-driven signaling — callers await these instead of polling
        self._result_events: Dict[str, asyncio.Event] = {}
        self._snapshot_event: asyncio.Event = asyncio.Event()
        self._snapshot_generation: int = 0
        self._extension_ws = None  # WebSocket to the extension for push

    @property
    def session_token(self) -> str:
        if self._session_token is None:
            self._session_token = _default_bridge_token()
        return self._session_token

    def is_connected(self) -> bool:
        """True only if a session is registered AND we've heard from the
        extension within the staleness window."""
        if not self._connected_session_id:
            return False
        if self._last_seen_at and (time.time() - self._last_seen_at > self.STALE_THRESHOLD):
            # Auto-disconnect stale session
            self.disconnect()
            return False
        return True

    def connected_session_id(self) -> Optional[str]:
        return self._connected_session_id

    def last_seen_at(self) -> float:
        return self._last_seen_at

    def extension_name(self) -> str:
        return self._extension_name

    def authenticate(self, token: str) -> bool:
        """
        Constant-time compare of the presented pairing code.

        Both sides are stripped so a copied code with a stray newline or space
        still matches — the content must still be exactly right, so this does
        not weaken the check. An empty token never authenticates.
        """
        presented = (token or "").strip()
        expected = (self.session_token or "").strip()
        if not presented or not expected:
            return False
        return secrets.compare_digest(presented, expected)

    def register_connection(self, session_id: str, extension_name: str = "") -> None:
        self._connected_session_id = session_id
        self._extension_name = extension_name
        self._last_seen_at = time.time()
        runtime_state_store.mark_browser_connected(session_id=session_id)

    def set_extension_ws(self, ws) -> None:
        """Store the extension WebSocket for server-push."""
        self._extension_ws = ws

    def clear_extension_ws(self) -> None:
        self._extension_ws = None

    def touch(self) -> None:
        self._last_seen_at = time.time()

    def register_snapshot(self, snapshot: PageSnapshot) -> None:
        browser_store.upsert_snapshot(snapshot)
        self._connected_session_id = snapshot.session_id
        self._last_seen_at = time.time()
        runtime_state_store.register_browser_snapshot(snapshot)
        # Signal any coroutines waiting for a new snapshot
        self._snapshot_generation = getattr(snapshot, "generation", 0)
        self._snapshot_event.set()
        self._snapshot_event = asyncio.Event()  # Reset for next waiter

    def queue_action(self, request: ActionRequest) -> ActionResult:
        if not self.is_connected():
            return ActionResult(
                ok=False,
                message="Browser extension bridge is not connected.",
                action=request.action,
                ref_id=request.ref_id,
            )

        snapshot = browser_store.get_snapshot(request.session_id or self._connected_session_id)
        if not snapshot:
            # Allow snapshot-independent actions (refresh_snapshot, evaluate_js)
            # to be queued even before the first snapshot arrives.
            _snapshotless_actions = {"refresh_snapshot", "evaluate_js", "extract_data", "extract_readability"}
            if request.action in _snapshotless_actions and (request.session_id or self._connected_session_id):
                request.session_id = request.session_id or self._connected_session_id or ""
                request.action_id = request.action_id or f"act_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
                self._result_events[request.action_id] = asyncio.Event()
                self._pending_actions.append(request)
                self._try_push_actions(request.session_id)
                return ActionResult(
                    ok=True,
                    message=f"{request.action} queued (no snapshot yet).",
                    action=request.action,
                    ref_id=request.ref_id,
                    action_id=request.action_id,
                    session_id=request.session_id,
                    pre_generation=0,
                    post_generation=0,
                )
            return ActionResult(
                ok=False,
                message="No active browser snapshot available.",
                action=request.action,
                ref_id=request.ref_id,
            )

        request.session_id = request.session_id or snapshot.session_id
        request.action_id = request.action_id or f"act_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
        self._result_events[request.action_id] = asyncio.Event()
        if request.action in {"evaluate_js", "extract_data", "extract_readability"}:
            request.metadata = {
                **request.metadata,
                "tab_id": request.metadata.get("tab_id", snapshot.tab_id),
                "generation": request.metadata.get("generation", str(snapshot.generation)),
            }
        if request.action == "refresh_snapshot":
            request.metadata = {
                **request.metadata,
                "tab_id": snapshot.tab_id,
                "generation": str(snapshot.generation),
            }
        
        if request.ref_id:
            element = browser_store.get_element(request.ref_id, request.session_id)
            if element:
                request.metadata = {
                    **request.metadata,
                    "tab_id": snapshot.tab_id,
                    "generation": str(snapshot.generation),
                    "agent_id": str(element.agent_id) if element.agent_id else "",
                    "dom_path": element.dom_path,
                    "role": element.role,
                    "tag": element.tag,
                    "label": element.primary_label(),
                    "text": element.text,
                    "aria_label": element.aria_label,
                    "name": element.name,
                    "placeholder": element.placeholder,
                    "href": element.href,
                }
        self._pending_actions.append(request)
        self._try_push_actions(request.session_id)
        return ActionResult(
            ok=True,
            message="Action queued for browser extension execution.",
            action=request.action,
            ref_id=request.ref_id,
            action_id=request.action_id,
            session_id=request.session_id,
            pre_generation=snapshot.generation,
            post_generation=snapshot.generation,
        )

    def drain_actions(self, session_id: Optional[str] = None) -> List[ActionRequest]:
        sid = session_id or self._connected_session_id
        if not sid:
            return []
        drained = [a for a in self._pending_actions if a.session_id == sid]
        self._pending_actions = [a for a in self._pending_actions if a.session_id != sid]
        return drained

    def clear_pending_actions(self) -> int:
        """Drop every queued (not yet delivered) browser action — used when the user cancels a task."""
        n = len(self._pending_actions)
        self._pending_actions.clear()
        return n

    def pending_action_count(self, session_id: Optional[str] = None) -> int:
        sid = session_id or self._connected_session_id
        if not sid:
            return 0
        return sum(1 for action in self._pending_actions if action.session_id == sid)

    def _try_push_actions(self, session_id: str) -> None:
        """Push pending actions to the extension WebSocket immediately."""
        import json as _json
        ws = self._extension_ws
        if ws is None:
            return
        actions = [a for a in self._pending_actions if a.session_id == session_id]
        if not actions:
            return
        try:
            payload = _json.dumps({
                "type": "browser_actions",
                "ok": True,
                "session_id": session_id,
                "actions": [a.__dict__ for a in actions],
            })
            # Drain these actions so they're not sent again on poll
            self._pending_actions = [a for a in self._pending_actions if a.session_id != session_id]
            # Fire-and-forget: schedule the send on the event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(ws.send(payload))
        except Exception:
            pass  # Fall back to polling

    def record_action_result(self, result: ActionResult) -> None:
        if result.action_id:
            self._latest_result_by_action_id[result.action_id] = result
            # Signal any coroutine waiting on this action
            evt = self._result_events.pop(result.action_id, None)
            if evt:
                evt.set()
        self._last_seen_at = time.time()
        runtime_state_store.record_browser_action_result(result)
        details = result.details or {}
        if result.action == "extract_readability" and isinstance(details, dict):
            raw_result = details.get("result", "")
            if raw_result:
                try:
                    import json
                    payload = json.loads(raw_result)
                except Exception:
                    payload = {"ok": False, "message": "invalid_readability_payload"}
                if isinstance(payload, dict):
                    runtime_state_store.record_readability_extraction(payload)

    def latest_action_result(self, action_id: str) -> Optional[ActionResult]:
        return self._latest_result_by_action_id.get(action_id)

    async def wait_for_result(self, action_id: str, timeout: float = 10.0) -> Optional[ActionResult]:
        """Await the action result instead of polling.  Returns None on timeout."""
        # Already recorded?
        existing = self._latest_result_by_action_id.get(action_id)
        if existing:
            self._result_events.pop(action_id, None)
            return existing
        evt = self._result_events.get(action_id)
        if not evt:
            return None
        try:
            await asyncio.wait_for(evt.wait(), timeout=max(0.05, timeout))
        except asyncio.TimeoutError:
            self._result_events.pop(action_id, None)
            return self._latest_result_by_action_id.get(action_id)
        self._result_events.pop(action_id, None)
        return self._latest_result_by_action_id.get(action_id)

    async def wait_for_snapshot(self, session_id: str = "", min_generation: int = 0, timeout: float = 2.0) -> Optional["PageSnapshot"]:
        """Await the next snapshot with generation > min_generation."""
        sid = session_id or self._connected_session_id
        # Check if we already have a satisfying snapshot
        current = browser_store.get_snapshot(sid)
        if current and current.generation > min_generation:
            return current
        deadline = time.time() + max(0.05, timeout)
        while time.time() < deadline:
            evt = self._snapshot_event
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(evt.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            current = browser_store.get_snapshot(sid)
            if current and current.generation > min_generation:
                return current
        return browser_store.get_snapshot(sid)

    def record_dom_change(self, event: DomChangeEvent) -> None:
        if event.action_id:
            self._latest_dom_change_by_action_id[event.action_id] = event
        self._last_seen_at = time.time()

    def latest_dom_change(self, action_id: str) -> Optional[DomChangeEvent]:
        return self._latest_dom_change_by_action_id.get(action_id)

    def disconnect(self) -> None:
        self._connected_session_id = None
        self._extension_name = ""
        runtime_state_store.mark_browser_disconnected()

    def reset(self) -> None:
        self._connected_session_id = None
        self._last_seen_at = 0.0
        self._pending_actions.clear()
        self._latest_result_by_action_id.clear()
        self._latest_dom_change_by_action_id.clear()
        self._result_events.clear()
        self._snapshot_event = asyncio.Event()
        self._snapshot_generation = 0
        self._extension_name = ""
        runtime_state_store.mark_browser_disconnected()


browser_bridge = BrowserBridge()
