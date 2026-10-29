from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..models import ToolExecutionResult, ToolRuntime
from ..utils import escape_applescript_string, osascript, run_exec, run_shell, write_text

APP_ALIASES: dict[str, str] = {
    "chrome": "Google Chrome",
    "safari": "Safari",
    "whatsapp": "WhatsApp",
    "slack": "Slack",
    "discord": "Discord",
    "notes": "Notes",
    "finder": "Finder",
    "messages": "Messages",
    "mail": "Mail",
    "terminal": "Terminal",
}


def candidate_app_names(app_name: str) -> list[str]:
    raw = str(app_name or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for candidate in (raw, APP_ALIASES.get(raw.lower(), ""), raw.title(), raw.replace(".app", "").strip()):
        cleaned = str(candidate or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


async def resolve_app_name(app_name: str) -> str:
    candidates = candidate_app_names(app_name)
    if not candidates:
        return app_name

    search_roots = ["/Applications", os.path.expanduser("~/Applications"), "/System/Applications"]
    installed: dict[str, str] = {}
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            if entry.endswith(".app"):
                installed.setdefault(entry[:-4].lower(), entry[:-4])

    for candidate in candidates:
        exact = installed.get(candidate.lower())
        if exact:
            return exact

    for candidate in candidates:
        code, stdout, _ = await run_exec(
            "mdfind",
            f'kMDItemKind == "Application" && kMDItemFSName == "{candidate}.app"',
            timeout=2.0,
        )
        if code == 0 and stdout:
            first_line = stdout.splitlines()[0].strip()
            if first_line:
                return Path(first_line).stem

    return candidates[0]


async def activate_application(app_name: str, runtime: ToolRuntime) -> ToolExecutionResult:
    if runtime.run_mode == "dry":
        runtime.state["active_app"] = app_name
        return ToolExecutionResult(ok=True, message=f"[dry-run] Activated {app_name}")

    resolved = await resolve_app_name(app_name)
    code, _, stderr = await run_exec("open", "-a", resolved, timeout=5.0)
    if code != 0:
        return ToolExecutionResult(ok=False, message=f"Failed to activate {resolved}: {stderr}")
    await asyncio.sleep(0.6)
    runtime.state["active_app"] = resolved
    return ToolExecutionResult(ok=True, message=f"Activated {resolved}", payload={"app_name": resolved})


async def current_frontmost_app() -> str:
    return await osascript(
        'tell application "System Events" to get name of first process whose frontmost is true',
        timeout=3.0,
    )


def tool_success(message: str, **payload) -> ToolExecutionResult:
    return ToolExecutionResult(ok=True, message=message, payload=payload)


def tool_failure(message: str, **payload) -> ToolExecutionResult:
    return ToolExecutionResult(ok=False, message=message, payload=payload)


def artifact_path(runtime: ToolRuntime, prefix: str, suffix: str) -> Path:
    filename = f"{int(time.time() * 1000)}_{prefix}{suffix}"
    return runtime.artifacts_dir / filename


def store_artifact(runtime: ToolRuntime, key: str, path: Path, content: Optional[str] = None) -> None:
    if content is not None:
        write_text(path, content)
    runtime.remember_artifact(key, str(path))


def remember_last_text(runtime: ToolRuntime, text: str) -> None:
    runtime.state["last_typed_text"] = str(text or "")[:500]


def last_typed_text(runtime: ToolRuntime) -> str:
    return str(runtime.state.get("last_typed_text", "") or "")


async def capture_screenshot(runtime: ToolRuntime, prefix: str = "screen") -> tuple[Optional[Path], str]:
    out_path = artifact_path(runtime, prefix, ".png")
    if runtime.run_mode == "dry":
        out_path.write_bytes(b"dry-run-image")
        runtime.remember_artifact("last_screenshot", str(out_path))
        return out_path, "[dry-run] screenshot captured"

    code, _, stderr = await run_exec("screencapture", "-x", "-t", "png", str(out_path), timeout=8.0)
    if code != 0:
        return None, f"Failed to capture screenshot: {stderr}"
    runtime.remember_artifact("last_screenshot", str(out_path))
    return out_path, f"Captured screenshot to {out_path.name}"


def encode_image_file(path: Path) -> bytes:
    return path.read_bytes()


def make_image_digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


async def click_point(x: int, y: int, click_type: str, runtime: ToolRuntime) -> ToolExecutionResult:
    if runtime.run_mode == "dry":
        runtime.state["last_pointer"] = {"x": x, "y": y}
        return tool_success(f"[dry-run] Clicked at ({x}, {y})", x=x, y=y, click_type=click_type)

    python_snippet = f"""
import time
try:
    import Quartz
except Exception as exc:
    raise SystemExit(f"Quartz unavailable: {{exc}}")

x, y = {x}, {y}
click_type = {click_type!r}

def mouse_event(event_type, px, py, button=Quartz.kCGMouseButtonLeft):
    event = Quartz.CGEventCreateMouseEvent(None, event_type, (px, py), button)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

if click_type == "right":
    mouse_event(Quartz.kCGEventRightMouseDown, x, y, Quartz.kCGMouseButtonRight)
    time.sleep(0.05)
    mouse_event(Quartz.kCGEventRightMouseUp, x, y, Quartz.kCGMouseButtonRight)
elif click_type == "double":
    mouse_event(Quartz.kCGEventLeftMouseDown, x, y)
    mouse_event(Quartz.kCGEventLeftMouseUp, x, y)
    time.sleep(0.05)
    event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 2)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 2)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
else:
    mouse_event(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(0.03)
    mouse_event(Quartz.kCGEventLeftMouseUp, x, y)
"""
    code, stdout, stderr = await run_exec("python3", "-c", python_snippet, timeout=5.0)
    if code != 0:
        return tool_failure(f"Failed to click at ({x}, {y}): {stderr or stdout}", x=x, y=y)
    runtime.state["last_pointer"] = {"x": x, "y": y}
    return tool_success(f"Clicked at ({x}, {y})", x=x, y=y, click_type=click_type)


async def move_mouse(x: int, y: int, runtime: ToolRuntime) -> ToolExecutionResult:
    if runtime.run_mode == "dry":
        runtime.state["last_pointer"] = {"x": x, "y": y}
        return tool_success(f"[dry-run] Moved mouse to ({x}, {y})", x=x, y=y)
    python_snippet = f"""
import Quartz
event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, ({x}, {y}), Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
"""
    code, stdout, stderr = await run_exec("python3", "-c", python_snippet, timeout=5.0)
    if code != 0:
        return tool_failure(f"Failed to move mouse: {stderr or stdout}", x=x, y=y)
    runtime.state["last_pointer"] = {"x": x, "y": y}
    return tool_success(f"Moved mouse to ({x}, {y})", x=x, y=y)


async def drag_mouse(x1: int, y1: int, x2: int, y2: int, runtime: ToolRuntime) -> ToolExecutionResult:
    if runtime.run_mode == "dry":
        runtime.state["last_pointer"] = {"x": x2, "y": y2}
        return tool_success(f"[dry-run] Dragged from ({x1}, {y1}) to ({x2}, {y2})", x1=x1, y1=y1, x2=x2, y2=y2)
    python_snippet = f"""
import time
import Quartz
start_pos = ({x1}, {y1})
end_pos = ({x2}, {y2})
move_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, start_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, move_event)
time.sleep(0.05)
down_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, start_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, down_event)
time.sleep(0.05)
drag_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, end_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, drag_event)
time.sleep(0.05)
up_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, end_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, up_event)
"""
    code, stdout, stderr = await run_exec("python3", "-c", python_snippet, timeout=5.0)
    if code != 0:
        return tool_failure(f"Failed to drag mouse: {stderr or stdout}", x1=x1, y1=y1, x2=x2, y2=y2)
    runtime.state["last_pointer"] = {"x": x2, "y": y2}
    return tool_success(f"Dragged mouse from ({x1}, {y1}) to ({x2}, {y2})", x1=x1, y1=y1, x2=x2, y2=y2)


async def scroll_lines(lines: int, runtime: ToolRuntime) -> ToolExecutionResult:
    if runtime.run_mode == "dry":
        return tool_success(f"[dry-run] Scrolled {lines} lines", lines=lines)
    python_snippet = f"""
import Quartz
event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, {lines})
Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
"""
    code, stdout, stderr = await run_exec("python3", "-c", python_snippet, timeout=5.0)
    if code != 0:
        return tool_failure(f"Failed to scroll: {stderr or stdout}", lines=lines)
    return tool_success(f"Scrolled {lines} lines", lines=lines)

