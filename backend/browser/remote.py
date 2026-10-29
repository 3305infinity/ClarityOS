"""
ClarityOS — Browser bridge owner proxy
======================================

Only one process can bind the extension's WebSocket port (8765): the hub or the
Electron voice server, whichever starts first. The loser held a BrowserBridge
with no extension attached, so every browser tool in that process failed even
though the extension was connected and authenticated — just to the other one.

Rather than add a second listener (which would split the extension's session)
or force a startup order, the process that *does* own the bridge exposes a
small control surface, and the one that does not borrows it. Both surfaces use
the same `tokens.json` bearer token every other local API already uses, so no
new credential is introduced.

The proxied surface mirrors what the browser tools actually call:

    status()    -> is_connected / connected_session_id
    snapshot()  -> wait_for_snapshot
    action()    -> queue_action + wait_for_result

Everything here fails closed: if no owner answers, callers get None and report
an honest "cannot reach the extension" rather than pretending to have looked.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

# Where an owner may be listening. The hub serves FastAPI on 8787; the voice
# server answers the same paths on 8000 through its websockets process_request
# hook. `self_port` lets a process skip asking itself.
HUB_PORT = int(os.environ.get("CLARITYOS_HUB_PORT", "8787"))
VOICE_PORT = int(os.environ.get("CLARITYOS_BACKEND_PORT", "8000"))

_DEFAULT_TIMEOUT = 10.0


def _ui_token() -> str:
    try:
        from hub.config import load_tokens
        return (load_tokens().get("ui") or "").strip()
    except Exception:
        return ""


def candidate_ports(self_port: int = 0) -> tuple[int, ...]:
    return tuple(p for p in (HUB_PORT, VOICE_PORT) if p and p != self_port)


def _request(port: int, path: str, payload: Optional[Dict[str, Any]], timeout: float) -> Optional[Dict[str, Any]]:
    token = _ui_token()
    if not token:
        return None
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    req = urllib.request.Request(url)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Host", f"127.0.0.1:{port}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


async def _call(path: str, payload: Optional[Dict[str, Any]], *,
                self_port: int = 0, timeout: float = _DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """
    Ask each candidate in turn and take the first that actually owns the
    extension.

    Reachability is not ownership: the process without the extension answers
    perfectly well with `connected: false` / `bridge_disconnected`. Taking that
    first answer would report "no extension" while the other process was
    holding a live, authenticated connection — so keep looking, and only fall
    back to the negative answer if nobody owns it.
    """
    loop = asyncio.get_running_loop()
    fallback: Optional[Dict[str, Any]] = None
    for port in candidate_ports(self_port):
        result = await loop.run_in_executor(None, _request, port, path, payload, timeout)
        if result is None:
            continue
        result = dict(result)
        result["_owner_port"] = port
        if "connected" in result:
            # /status always answers ok:true, so only the flag decides. Without
            # this the non-owner's honest "connected: false" was accepted as
            # the answer while the real owner sat on the next port.
            owns = result.get("connected") is True
        else:
            owns = result.get("ok") is True and result.get("error") != "bridge_disconnected"
        if owns:
            return result
        if fallback is None:
            fallback = result
    return fallback


async def remote_status(self_port: int = 0, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    """{'connected': bool, 'session_id': str} from whichever process owns the bridge."""
    return await _call("/api/browser/status", None, self_port=self_port, timeout=timeout)


async def remote_snapshot(session_id: str = "", self_port: int = 0,
                          timeout: float = _DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    path = "/api/browser/snapshot"
    if session_id:
        path += "?session_id=" + urllib.parse.quote(session_id)
    return await _call(path, None, self_port=self_port, timeout=timeout)


async def remote_action(action: Dict[str, Any], self_port: int = 0,
                        timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    """Queue one ActionRequest on the owner and wait for its real result."""
    return await _call("/api/browser/action", {"action": action, "timeout": timeout},
                       self_port=self_port, timeout=timeout + 5.0)


async def remote_page(session_id: str = "", self_port: int = 0,
                      timeout: float = 12.0) -> Optional[Dict[str, Any]]:
    path = "/api/browser/page"
    if session_id:
        path += "?session_id=" + urllib.parse.quote(session_id)
    return await _call(path, None, self_port=self_port, timeout=timeout)
