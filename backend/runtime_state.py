"""
Moonwalk — Canonical runtime state store.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class OSState:
    active_app: str = ""
    window_title: str = ""
    clipboard: str = ""
    browser_url: str = ""
    browser_url_provenance: str = ""
    browser_url_degraded: bool = False
    updated_at: float = 0.0


@dataclass
class BrowserState:
    connected: bool = False
    session_id: str = ""
    tab_id: str = ""
    url: str = ""
    title: str = ""
    generation: int = 0
    last_snapshot_at: float = 0.0
    last_action_at: float = 0.0
    last_action: str = ""
    last_action_ok: bool = False
    last_action_error_code: str = ""
    last_readability: dict[str, Any] = field(default_factory=dict)


@dataclass
class RequestState:
    request_id: str = ""
    query: str = ""
    selected_source_url: str = ""
    selected_source_label: str = ""
    active_doc_url: str = ""
    search_results: list[dict[str, Any]] = field(default_factory=list)
    extracted_content: dict[str, Any] = field(default_factory=dict)
    pending_confirmation: str = ""
    updated_at: float = 0.0


@dataclass
class SessionState:
    opened_urls: list[str] = field(default_factory=list)
    recent_artifacts: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


@dataclass
class RuntimeStateSnapshot:
    os_state: OSState
    browser_state: BrowserState
    request_state: RequestState
    session_state: SessionState

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeStateStore:
    """Thread-safe canonical runtime state singleton.

    All mutations are guarded by a lock so that concurrent
    WebSocket handlers, background tasks, and agent coroutines
    never observe a half-updated state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._os_state = OSState()
        self._browser_state = BrowserState()
        self._request_state = RequestState()
        self._session_state = SessionState()

    def reset(self) -> None:
        with self._lock:
            self._os_state = OSState()
            self._browser_state = BrowserState()
            self._request_state = RequestState()
            self._session_state = SessionState()

    def snapshot(self) -> RuntimeStateSnapshot:
        with self._lock:
            return RuntimeStateSnapshot(
                os_state=copy.deepcopy(self._os_state),
                browser_state=copy.deepcopy(self._browser_state),
                request_state=copy.deepcopy(self._request_state),
                session_state=copy.deepcopy(self._session_state),
            )

    def update_os_state(
        self,
        *,
        active_app: str = "",
        window_title: str = "",
        clipboard: str = "",
        browser_url: str = "",
        provenance: str = "",
        degraded: bool = False,
    ) -> None:
        with self._lock:
            now = time.time()
            if active_app:
                self._os_state.active_app = active_app
            if window_title:
                self._os_state.window_title = window_title
            if clipboard:
                self._os_state.clipboard = clipboard
            if browser_url:
                self._os_state.browser_url = browser_url
                self._os_state.browser_url_provenance = provenance or ""
                self._os_state.browser_url_degraded = bool(degraded)
            self._os_state.updated_at = now

    def mark_browser_connected(self, *, session_id: str = "") -> None:
        with self._lock:
            self._browser_state.connected = True
            if session_id:
                self._browser_state.session_id = session_id

    def mark_browser_disconnected(self) -> None:
        with self._lock:
            self._browser_state.connected = False

    def register_browser_snapshot(self, snapshot: Any) -> None:
        with self._lock:
            self._browser_state.connected = True
            self._browser_state.session_id = str(getattr(snapshot, "session_id", "") or "")
            self._browser_state.tab_id = str(getattr(snapshot, "tab_id", "") or "")
            self._browser_state.url = str(getattr(snapshot, "url", "") or "")
            self._browser_state.title = str(getattr(snapshot, "title", "") or "")
            self._browser_state.generation = int(getattr(snapshot, "generation", 0) or 0)
            self._browser_state.last_snapshot_at = time.time()
            if self._browser_state.url:
                self._os_state.browser_url = self._browser_state.url
                self._os_state.browser_url_provenance = "browser_bridge"
                self._os_state.browser_url_degraded = False

    def record_browser_action_result(self, result: Any) -> None:
        with self._lock:
            self._browser_state.connected = True
            self._browser_state.session_id = str(getattr(result, "session_id", "") or self._browser_state.session_id)
            self._browser_state.last_action = str(getattr(result, "action", "") or "")
            self._browser_state.last_action_ok = bool(getattr(result, "ok", False))
            self._browser_state.last_action_at = time.time()
            error = getattr(result, "error", {}) or {}
            if isinstance(error, dict):
                self._browser_state.last_action_error_code = str(error.get("code", "") or "")
            else:
                self._browser_state.last_action_error_code = ""
            details = getattr(result, "details", {}) or {}
            if isinstance(details, dict) and details.get("result"):
                self._browser_state.last_readability = {"raw_result": details.get("result")}

    def record_readability_extraction(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._browser_state.last_readability = copy.deepcopy(payload or {})

    def start_request(self, *, request_id: str = "", query: str = "") -> None:
        with self._lock:
            self._request_state = RequestState(
                request_id=request_id or self._request_state.request_id,
                query=query or "",
                updated_at=time.time(),
            )

    def update_request_state(self, **updates: Any) -> None:
        with self._lock:
            for key, value in updates.items():
                if hasattr(self._request_state, key):
                    setattr(self._request_state, key, value)
            self._request_state.updated_at = time.time()

    def remember_opened_url(self, url: str) -> None:
        if not url:
            return
        with self._lock:
            if url not in self._session_state.opened_urls:
                self._session_state.opened_urls.append(url)
            self._session_state.updated_at = time.time()

    def remember_artifact(self, key: str, value: Any) -> None:
        if not key:
            return
        with self._lock:
            self._session_state.recent_artifacts[key] = copy.deepcopy(value)
            self._session_state.updated_at = time.time()

    # ── Request cancellation ──────────────────────────────────

    def cancel_request(self, request_id: str = "") -> bool:
        """Mark the active request as cancelled.

        Returns True if the request_id matched (or none was specified)
        and the cancellation was recorded, False otherwise.
        """
        with self._lock:
            current = self._request_state.request_id
            if request_id and current and request_id != current:
                return False
            self._request_state.pending_confirmation = "cancelled"
            self._request_state.updated_at = time.time()
            return True

    def is_request_cancelled(self, request_id: str = "") -> bool:
        """Check whether the active request has been cancelled."""
        with self._lock:
            if request_id and self._request_state.request_id != request_id:
                return False
            return self._request_state.pending_confirmation == "cancelled"


# ═══════════════════════════════════════════════════════════════
#  Per-User Store Registry (for cloud multi-user mode)
# ═══════════════════════════════════════════════════════════════

_user_stores: dict[str, RuntimeStateStore] = {}
_registry_lock = threading.Lock()


def get_runtime_state(user_id: str = "") -> RuntimeStateStore:
    """Get or create a RuntimeStateStore for a specific user.

    In local (single-user) mode, pass no user_id to get the global
    singleton.  In cloud mode, each user_id gets an isolated store
    so concurrent sessions never collide.
    """
    if not user_id:
        return runtime_state_store

    with _registry_lock:
        if user_id not in _user_stores:
            _user_stores[user_id] = RuntimeStateStore()
        return _user_stores[user_id]


def evict_runtime_state(user_id: str) -> bool:
    """Remove a user's runtime state store (e.g. after disconnect timeout)."""
    with _registry_lock:
        return _user_stores.pop(user_id, None) is not None


# Default global singleton (backward-compatible for local mode)
runtime_state_store = RuntimeStateStore()
