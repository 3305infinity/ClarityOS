"""
Hub side of the Windows helper connection.

The helper is a separate, user-started process (windows_helper/) that connects OUT to the hub
over an authenticated WebSocket. The hub never reaches into the desktop by itself: desktop
tools are proxied to the helper through `HelperLink.call()`. If the helper is not running,
desktop tools fail with a clear message and nothing touches the desktop.

Wire protocol (JSON):
  helper -> hub   {"type":"hello","token","version","host","os","elevated","tools":[...]}
                  {"type":"result","id","ok":bool,"result":any,"error":str}
                  {"type":"emergency_stop"}          (user pressed the helper kill-switch)
                  {"type":"pong"}
  hub -> helper   {"type":"hello_ack","ok":bool,"message"}
                  {"type":"call","id","tool","args","timeout"}
                  {"type":"cancel"}                  (abort whatever is running + disarm)
                  {"type":"arm"}                     (new task: re-enable after a stop)
                  {"type":"ping"}
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol


class JsonSocket(Protocol):
    async def send_json(self, data: Dict[str, Any]) -> None: ...


class HelperError(Exception):
    pass


class HelperNotConnected(HelperError):
    pass


NOT_CONNECTED_MSG = (
    "ERROR: The Windows helper is not connected, so desktop control is unavailable. "
    "Ask the user to start it (windows_helper\\start.ps1) and check Settings → Connections."
)


class HelperLink:
    def __init__(self, on_change: Optional[Callable[[], Awaitable[None]]] = None,
                 on_emergency_stop: Optional[Callable[[], Awaitable[None]]] = None):
        self._ws: Optional[JsonSocket] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self.info: Dict[str, Any] = {}
        self.tools: set[str] = set()
        self._on_change = on_change
        self._on_stop = on_emergency_stop

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def status(self) -> Dict[str, Any]:
        return {"connected": self.connected, **({k: v for k, v in self.info.items()} if self.connected else {})}

    async def attach(self, ws: JsonSocket, hello: Dict[str, Any]) -> None:
        if self._ws is not None and self._ws is not ws:
            await self.fail_all("replaced by a newer helper connection")
        self._ws = ws
        self.tools = set(hello.get("tools") or [])
        self.info = {
            "version": str(hello.get("version", ""))[:40],
            "host": str(hello.get("host", ""))[:80],
            "os": str(hello.get("os", ""))[:120],
            "elevated": bool(hello.get("elevated", False)),
            "tools": sorted(self.tools),
        }
        if self._on_change:
            await self._on_change()

    async def detach(self, ws: JsonSocket) -> None:
        if self._ws is ws:
            self._ws = None
            self.info = {}
            self.tools = set()
            await self.fail_all("Windows helper disconnected")
            if self._on_change:
                await self._on_change()

    async def fail_all(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(HelperError(reason))
        self._pending.clear()

    async def call(self, tool: str, args: Dict[str, Any], timeout: float = 45.0) -> Any:
        ws = self._ws
        if ws is None:
            raise HelperNotConnected(NOT_CONNECTED_MSG)
        if self.tools and tool not in self.tools:
            raise HelperError(f"The connected helper does not support '{tool}' (update the helper).")
        cid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            await ws.send_json({"type": "call", "id": cid, "tool": tool, "args": args, "timeout": timeout})
            return await asyncio.wait_for(fut, timeout=timeout + 2.0)
        except asyncio.TimeoutError:
            raise HelperError(f"Windows helper timed out running '{tool}' after {timeout:.0f}s")
        finally:
            self._pending.pop(cid, None)

    async def handle_message(self, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "result":
            fut = self._pending.get(str(msg.get("id", "")))
            if fut and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(HelperError(str(msg.get("error") or "helper error")))
        elif t == "emergency_stop":
            if self._on_stop:
                await self._on_stop()

    async def cancel_all(self, reason: str = "cancelled") -> None:
        """Propagate a user cancel: abort in-flight helper actions and disarm until the next task."""
        ws = self._ws
        if ws is not None:
            try:
                await ws.send_json({"type": "cancel"})
            except Exception:
                pass
        await self.fail_all(reason)

    async def arm(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send_json({"type": "arm"})
            except Exception:
                pass
