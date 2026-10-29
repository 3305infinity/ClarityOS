"""
ClarityOS — Windows desktop tools for the Electron voice server
===============================================================

Why this exists
---------------
`hub/desktop_proxy.py` rebinds the macOS tools (`open_app`, `get_ui_tree`,
`type_text`, …) onto the Windows helper, but it is only installed by the **hub**
process, because it needs `hub.helper` — the hub's WebSocket link to the helper.

The Electron voice server (`servers/local_server.py`) runs its own agent and
never installed it. So "Open Spotify" typed into the overlay or the sidebar
reached the *macOS* `open_app`, which shells out to `osascript` and fails on
Windows. The task looked like it ran and then did nothing.

This module closes that gap by giving the voice server the same tool rebinding
through a link that dispatches **in-process** to `windows_helper/desktop.py`
instead of over a socket. It reuses `desktop_proxy.install()` verbatim, so the
proxy semantics, the AppleScript stubs and the macOS-only tool removals are
identical to the hub's.

Security: this changes *where a tool runs*, never *whether it is allowed*. The
voice server installs the PolicyEngine + ApprovalBroker guard on the registry
(`servers/voice_guard.py`) before this runs, so every call is still classified
and still needs approval when the policy says so.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

# The helper lives outside the backend package; it is a plain module.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELPER_DIR = _REPO_ROOT / "windows_helper"


class LocalDesktopLink:
    """
    A HelperLink-shaped object that runs desktop actions in this process.

    `desktop_proxy.install()` only ever calls `link.call(tool, args)`, so this
    is all the surface it needs. Calls run on a single worker thread — one
    mouse, one keyboard — exactly like the helper process does.
    """

    def __init__(self, desktop_module) -> None:
        self._desktop = desktop_module
        self.tools: Set[str] = set(desktop_module.TOOLS.keys())
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="clarityos-desktop"
        )
        # A timed-out call whose thread is still running; see `call`.
        # Completion is signalled by the worker, not by the asyncio future:
        # wait_for cancels that future, so its .done() is True immediately even
        # though the thread keeps going.
        self._stuck_tool = ""
        self._worker_idle = threading.Event()
        self._worker_idle.set()

    @property
    def connected(self) -> bool:
        """
        Always true: the "link" is this process. `desktop_proxy` checks this
        before dispatching (and to decide whether a failure means "no helper"),
        so it has to exist or every proxied call raises AttributeError after the
        tool has already run.
        """
        return True

    @property
    def info(self) -> Dict[str, Any]:
        return {"host": "local", "transport": "in-process", "tools": sorted(self.tools)}

    def snapshot(self) -> Dict[str, Any]:
        return {"connected": True, **self.info}

    def _run(self, tool: str, args: Dict[str, Any]) -> Any:
        self._worker_idle.clear()
        try:
            return self._run_inner(tool, args)
        finally:
            self._worker_idle.set()

    def _run_inner(self, tool: str, args: Dict[str, Any]) -> Any:
        entry = self._desktop.TOOLS.get(tool)
        if entry is None:
            return f"ERROR: '{tool}' is not available on this machine."
        func = entry[0]
        try:
            return func(**args)
        except self._desktop.ToolError as exc:
            return f"ERROR: {exc}"
        except TypeError as exc:
            return f"ERROR: bad arguments for {tool} ({exc})"
        except Exception as exc:                      # never let a tool kill the agent
            return f"ERROR: {exc.__class__.__name__}: {str(exc)[:200]}"

    async def call(self, tool: str, args: Dict[str, Any], timeout: float = 45.0) -> Any:
        if tool not in self.tools:
            return f"ERROR: '{tool}' is not supported by the local desktop tools."

        # asyncio.wait_for cancels the *await*, never the synchronous function
        # already running in the worker thread. With a single worker, a call
        # that timed out keeps the thread busy and every later desktop call
        # silently queues behind it — the next request appears to hang. Fail
        # fast instead, and say so, until the stuck call actually returns.
        if self._stuck_tool and not self._worker_idle.is_set():
            return (f"ERROR: a previous desktop operation ('{self._stuck_tool}') is still "
                    f"running and cannot be interrupted. Desktop tools are unavailable "
                    f"until it finishes.")
        self._stuck_tool = ""

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, self._run, tool, dict(args or {}))
        try:
            return await asyncio.wait_for(future, max(1.0, min(float(timeout), 120.0)))
        except asyncio.TimeoutError:
            # Remember the abandoned call so later ones fail fast rather than
            # queueing behind a thread nobody can stop.
            self._stuck_tool = tool
            # Deliberately NOT setting desktop.CANCEL here. In the helper
            # process that flag is cleared by the hub's "arm" message before
            # each task; in-process nothing clears it, so one slow first call
            # (COM/UIA init easily exceeds perception's 3s budget) latched the
            # stop flag and every later tool returned "Stopped by the user",
            # taking the whole request down with it. The worker thread finishes
            # on its own; just report the timeout.
            return f"ERROR: '{tool}' timed out."

    def cancel(self) -> None:
        try:
            self._desktop.CANCEL.set()
        except Exception:
            pass


def _import_desktop():
    if os.name != "nt":
        return None, "not Windows"
    if not _HELPER_DIR.exists():
        return None, f"windows_helper not found at {_HELPER_DIR}"
    if str(_HELPER_DIR) not in sys.path:
        sys.path.insert(0, str(_HELPER_DIR))
    try:
        import desktop  # type: ignore
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    return desktop, ""


_link: Optional[LocalDesktopLink] = None


def install_local_desktop_tools(registry) -> str:
    """
    Rebind the proxied desktop tools onto this process.

    Returns "" on success or a human-readable reason it was skipped. Safe to
    call once at startup; `desktop_proxy.install` is itself idempotent.
    """
    global _link

    desktop_module, reason = _import_desktop()
    if desktop_module is None:
        return reason

    try:
        from hub import desktop_proxy
    except Exception as exc:
        return f"desktop_proxy unavailable ({exc.__class__.__name__})"

    try:
        desktop_module.init_process()
    except Exception:
        # DPI / COM init is best-effort; the individual tools report their own
        # errors if the environment really is unusable.
        pass

    _link = LocalDesktopLink(desktop_module)
    # Start from a clean slate: a stop flag left set by a previous run would
    # otherwise make every desktop tool refuse before it even ran.
    try:
        desktop_module.CANCEL.clear()
        desktop_module.STOPPED.clear()
    except Exception:
        pass
    desktop_proxy.install(_link, registry)
    return ""


def cancel_local_desktop() -> None:
    """Stop an in-flight desktop action (wired to the agent's cancel path)."""
    if _link is not None:
        _link.cancel()
