"""
ClarityOS — Browser bridge owner endpoints (shared implementation)
==================================================================

The hub serves these through FastAPI; the Electron voice server serves the same
paths through its websockets `process_request` hook. Both call straight into
this module so the two surfaces cannot drift apart.

These run in the process that actually owns port 8765, i.e. the one the Chrome
extension is authenticated to. Authentication for the *caller* is the local
`tokens.json` UI bearer token, checked by each transport before it gets here.
"""

from __future__ import annotations

import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional

from .bridge import browser_bridge
from .models import ActionRequest


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def status() -> Dict[str, Any]:
    """Is an extension attached to *this* process, and under which session."""
    return {
        "ok": True,
        "connected": browser_bridge.is_connected(),
        "session_id": browser_bridge.connected_session_id() or "",
        "last_seen_at": browser_bridge.last_seen_at(),
        "served_at": time.time(),
    }


async def snapshot(session_id: str = "", timeout: float = 8.0) -> Dict[str, Any]:
    """Latest DOM snapshot of the extension's active tab."""
    if not browser_bridge.is_connected():
        return {"ok": False, "error": "bridge_disconnected",
                "message": "No Chrome extension is connected to this ClarityOS process."}
    snap = await browser_bridge.wait_for_snapshot(session_id=session_id, timeout=timeout)
    if snap is None:
        return {"ok": False, "error": "snapshot_timeout",
                "message": f"The extension sent no snapshot within {timeout:g}s."}
    return {"ok": True, "snapshot": _plain(snap)}


async def action(payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    """
    Queue one ActionRequest on the owner's bridge and return its real result.

    This is the generic path: clicks, navigation, element lookup and readability
    extraction all travel through ActionRequest, so proxying it covers the whole
    tool surface rather than page reading alone.
    """
    if not browser_bridge.is_connected():
        return {"ok": False, "error": "bridge_disconnected",
                "message": "No Chrome extension is connected to this ClarityOS process."}

    data = dict(payload or {})
    try:
        request = ActionRequest(
            action=str(data.get("action", "")),
            ref_id=str(data.get("ref_id", "") or ""),
            session_id=str(data.get("session_id", "") or (browser_bridge.connected_session_id() or "")),
            timeout=float(data.get("timeout", timeout) or timeout),
            **{k: v for k, v in data.items()
               if k in ("value", "text", "url", "direction", "amount", "metadata") and v is not None},
        )
    except TypeError as exc:
        return {"ok": False, "error": "bad_action", "message": f"Invalid action payload: {exc}"}

    queued = browser_bridge.queue_action(request)
    if not queued.ok:
        return {"ok": False, "error": "queue_failed", "message": queued.message}

    result = await browser_bridge.wait_for_result(queued.action_id, timeout=timeout)
    if result is None:
        return {"ok": False, "error": "action_timeout",
                "message": f"The extension returned no result within {timeout:g}s."}
    return {"ok": bool(result.ok), "result": _plain(result)}


async def page(session_id: str = "", timeout: float = 10.0) -> Dict[str, Any]:
    """Readable text of the active tab, via the existing extraction path."""
    if not browser_bridge.is_connected():
        return {"ok": False, "error": "bridge_disconnected",
                "message": "No Chrome extension is connected to this ClarityOS process."}
    from tools.browser_tools import _bridge_extract_readability
    return await _bridge_extract_readability(session_id=session_id, timeout=timeout)
