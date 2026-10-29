r"""
Moonwalk Windows helper
=======================
A small, user-started program that lets the Moonwalk Hub control THIS Windows PC.

  * connects OUT to the hub (default ws://127.0.0.1:8787/helper) with a private token
  * runs only the tool groups you allow:   perceive (screenshots, UI tree)  input (mouse/keyboard/apps)
    shell (PowerShell — off unless you pass --allow-shell)
  * refuses UAC / secure-desktop prompts and elevated windows (never bypasses Windows security)
  * kill-switch: press  Ctrl + Alt + Esc  (or slam the mouse into the top-left corner) to stop at once
  * reconnects automatically if the hub restarts

    py helper.py                         # same PC as the hub: token is read from %APPDATA%\Moonwalk\tokens.json
    py helper.py --hub wss://host/helper --token <helper token>
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desktop  # noqa: E402

GROUPS = {"perceive", "input", "shell"}
KILL_COMBO_NAME = "Ctrl+Alt+Esc"


def default_token() -> str:
    env = os.environ.get("MOONWALK_HELPER_TOKEN", "").strip()
    if env:
        return env
    base = os.environ.get("MOONWALK_USER_DIR") or os.path.join(os.environ.get("APPDATA", str(Path.home())), "Moonwalk")
    try:
        return json.loads(Path(base, "tokens.json").read_text(encoding="utf-8")).get("helper", "")
    except (OSError, ValueError):
        return ""


def allowed_tools(allow: Set[str]) -> Dict[str, Any]:
    return {name: fn for name, (fn, group) in desktop.TOOLS.items() if group in allow}


def run_tool(fn, args: Dict[str, Any]) -> Any:
    """Executes inside the single worker thread. Never raises: errors become 'ERROR: …' text for the agent."""
    try:
        res = fn(**args)
        return res
    except desktop.ToolError as e:
        return f"ERROR: {e}"
    except TypeError as e:
        return f"ERROR: bad arguments ({e})"
    except Exception as e:  # pyautogui.FailSafeException etc.
        name = e.__class__.__name__
        if name == "FailSafeException":
            desktop.STOPPED.set()
            return "ERROR: Stopped — the mouse was moved to the top-left corner (fail-safe)."
        return f"ERROR: {name}: {str(e)[:200]}"


def _init_worker() -> None:
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def start_killswitch(loop: asyncio.AbstractEventLoop, notify) -> None:
    """Polls Ctrl+Alt+Esc; on press: stop everything locally, then tell the hub."""
    if not desktop.IS_WIN:
        return
    import ctypes
    user32 = ctypes.windll.user32

    def down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def loop_fn():
        while True:
            time.sleep(0.05)
            if down(0x11) and down(0x12) and down(0x1B):       # Ctrl + Alt + Esc
                desktop.STOPPED.set()
                desktop.CANCEL.set()
                print(f"[helper] ⛔ kill-switch ({KILL_COMBO_NAME}) — all desktop actions stopped")
                asyncio.run_coroutine_threadsafe(notify(), loop)
                time.sleep(1.0)

    threading.Thread(target=loop_fn, daemon=True, name="killswitch").start()


async def session(url: str, token: str, allow: Set[str], killswitch: bool) -> None:
    import websockets
    tools = allowed_tools(allow)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=_init_worker)   # ONE mouse, ONE keyboard
    async with websockets.connect(url, max_size=16 * 1024 * 1024, ping_interval=20, ping_timeout=40, open_timeout=10) as ws:
        send_lock = asyncio.Lock()

        async def send(obj: Dict[str, Any]) -> None:
            async with send_lock:
                await ws.send(json.dumps(obj))

        await send({"type": "hello", "token": token, "version": desktop.VERSION, "host": socket.gethostname(),
                    "os": f"{platform.system()} {platform.release()} ({platform.version()})",
                    "elevated": desktop.is_admin(), "tools": sorted(tools)})
        ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if not ack.get("ok"):
            raise PermissionError(f"hub refused the helper: {ack.get('message')}")
        print(f"[helper] ✓ connected to {url} — tools: {', '.join(sorted(tools))}")
        if killswitch:
            start_killswitch(asyncio.get_running_loop(), lambda: send({"type": "emergency_stop"}))
        in_flight: Set[asyncio.Task] = set()

        async def handle_call(msg: Dict[str, Any]) -> None:
            cid, name = msg.get("id"), msg.get("tool")
            fn = tools.get(name)
            if fn is None:
                await send({"type": "result", "id": cid, "ok": True,
                            "result": f"ERROR: '{name}' is not enabled on this helper (allowed groups: {', '.join(sorted(allow))})"})
                return
            args = msg.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            print(f"[helper] → {name}({', '.join(args.keys())})")        # arg VALUES are never logged (privacy)
            timeout = max(1.0, min(float(msg.get("timeout", 30)), 120.0))
            fut = asyncio.get_running_loop().run_in_executor(pool, run_tool, fn, args)
            try:
                result = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                desktop.CANCEL.set()
                result = f"ERROR: '{name}' timed out and was aborted"
            await send({"type": "result", "id": cid, "ok": True, "result": result})

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                t = msg.get("type")
                if t == "call":
                    task = asyncio.create_task(handle_call(msg))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                elif t == "cancel":
                    desktop.CANCEL.set()
                    print("[helper] cancel requested by hub")
                elif t == "arm":
                    desktop.CANCEL.clear()
                    desktop.STOPPED.clear()
                elif t == "ping":
                    await send({"type": "pong"})
        finally:
            for t in list(in_flight):
                t.cancel()
            pool.shutdown(wait=False, cancel_futures=True)


async def main_async(args) -> None:
    allow = set(a.strip() for a in args.allow.split(",") if a.strip()) & GROUPS
    if args.allow_shell:
        allow.add("shell")
    token = args.token or default_token()
    if not token:
        print("No helper token found. Start the hub once (it creates tokens.json) or pass --token.")
        return
    print(f"Moonwalk Windows helper {desktop.VERSION}  |  allowed: {', '.join(sorted(allow)) or 'nothing'}")
    print(f"Kill-switch: {KILL_COMBO_NAME}  (or move the mouse to the top-left screen corner)")
    if desktop.is_admin():
        print("NOTE: running as Administrator — this lets you control elevated apps; only do this on purpose.")
    delay = 1.0
    while True:
        try:
            await session(args.hub, token, allow, not args.no_killswitch)
            delay = 1.0
        except PermissionError as e:
            print(f"[helper] ✗ {e}")
            return
        except Exception as e:
            print(f"[helper] disconnected ({e.__class__.__name__}: {str(e)[:120]}). Retrying in {delay:.0f}s…")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)


def main() -> None:
    p = argparse.ArgumentParser(description="Moonwalk Windows helper")
    p.add_argument("--hub", default=os.environ.get("MOONWALK_HUB_URL", "ws://127.0.0.1:8787/helper"))
    p.add_argument("--token", default="")
    p.add_argument("--allow", default="perceive,input", help="comma list of: perceive,input,shell")
    p.add_argument("--allow-shell", action="store_true", help="also allow PowerShell commands (hub still asks you each time)")
    p.add_argument("--no-killswitch", action="store_true")
    args = p.parse_args()
    if not desktop.IS_WIN:
        print("The Windows helper only works on Windows (desktop control needs the Win32 APIs).")
        sys.exit(1)
    desktop.init_process()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[helper] stopped")


if __name__ == "__main__":
    main()
