from __future__ import annotations

import asyncio

from ..models import ToolExecutionResult, ToolRuntime
from ..toolbox import ExperimentTool
from ..utils import escape_applescript_string, osascript, run_exec
from .common import (
    activate_application,
    click_point,
    drag_mouse,
    last_typed_text,
    move_mouse,
    remember_last_text,
    scroll_lines,
    tool_failure,
    tool_success,
)


async def _activate_app(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    app_name = str(args.get("app_name", "") or "").strip()
    if not app_name:
        return tool_failure("app_name is required")
    return await activate_application(app_name, runtime)


async def _type_text(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    text = str(args.get("text", "") or "")
    if not text:
        remembered = last_typed_text(runtime)
        if remembered:
            text = remembered
        else:
            return tool_failure("text is required")

    if runtime.run_mode == "dry":
        remember_last_text(runtime, text)
        return tool_success(f"[dry-run] Typed {len(text)} chars", text=text)

    if len(text) > 50:
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(input=text.encode("utf-8")), timeout=3.0)
        if proc.returncode != 0:
            return tool_failure(f"Failed to write clipboard: {err.decode('utf-8', errors='replace')[:200]}")
        result = await _run_shortcut({"keys": "command+v"}, runtime)
        if not result.ok:
            return result
        remember_last_text(runtime, text)
        return tool_success(f"Pasted {len(text)} chars into the active field", text=text)

    script = f'tell application "System Events" to keystroke "{escape_applescript_string(text)}"'
    output = await osascript(script, timeout=5.0)
    if output.lower().startswith("applescript error"):
        return tool_failure(output)
    remember_last_text(runtime, text)
    return tool_success(f"Typed {len(text)} chars into the active field", text=text)


async def _press_key(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    key = str(args.get("key", "") or "").strip().lower()
    times = max(1, min(20, int(args.get("times", 1) or 1)))
    key_map = {
        "return": "return",
        "tab": "tab",
        "space": "space",
        "escape": "escape",
        "up": "126",
        "down": "125",
        "left": "123",
        "right": "124",
        "delete": "51",
    }
    if key not in key_map:
        return tool_failure(f"Unsupported key '{key}'")
    if runtime.run_mode == "dry":
        return tool_success(f"[dry-run] Pressed {key} {times} time(s)", key=key, times=times)
    action = f"key code {key_map[key]}" if key in {"up", "down", "left", "right", "delete"} else f"keystroke {key_map[key]}"
    script = (
        'tell application "System Events"\n'
        f"repeat {times} times\n"
        f"  {action}\n"
        "  delay 0.05\n"
        "end repeat\n"
        "end tell"
    )
    output = await osascript(script, timeout=5.0)
    if output.lower().startswith("applescript error"):
        return tool_failure(output)
    return tool_success(f"Pressed '{key}' {times} time(s)", key=key, times=times)


async def _run_shortcut(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    keys = str(args.get("keys", "") or "").strip().lower()
    if not keys:
        return tool_failure("keys is required")
    parts = [part.strip() for part in keys.split("+") if part.strip()]
    if not parts:
        return tool_failure("keys is required")
    key = parts[-1]
    modifiers = parts[:-1]
    modifier_map = {
        "command": "command down",
        "shift": "shift down",
        "option": "option down",
        "alt": "option down",
        "control": "control down",
        "ctrl": "control down",
    }
    apple_modifiers = [modifier_map[m] for m in modifiers if m in modifier_map]
    modifier_block = f" using {{{', '.join(apple_modifiers)}}}" if apple_modifiers else ""
    if runtime.run_mode == "dry":
        return tool_success(f"[dry-run] Pressed shortcut {keys}", keys=keys)
    if len(key) == 1:
        script = f'tell application "System Events" to keystroke "{escape_applescript_string(key)}"{modifier_block}'
    else:
        key_codes = {"return": "36", "tab": "48", "space": "49", "escape": "53"}
        key_code = key_codes.get(key)
        if not key_code:
            return tool_failure(f"Unsupported shortcut key '{key}'")
        script = f'tell application "System Events" to key code {key_code}{modifier_block}'
    output = await osascript(script, timeout=5.0)
    if output.lower().startswith("applescript error"):
        return tool_failure(output)
    return tool_success(f"Pressed shortcut {keys}", keys=keys)


async def _click_point(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    x = int(args.get("x", 0) or 0)
    y = int(args.get("y", 0) or 0)
    click_type = str(args.get("click_type", "single") or "single")
    return await click_point(x, y, click_type, runtime)


async def _move_mouse(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    return await move_mouse(int(args.get("x", 0) or 0), int(args.get("y", 0) or 0), runtime)


async def _drag_mouse(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    return await drag_mouse(
        int(args.get("x1", 0) or 0),
        int(args.get("y1", 0) or 0),
        int(args.get("x2", 0) or 0),
        int(args.get("y2", 0) or 0),
        runtime,
    )


async def _scroll_view(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    return await scroll_lines(int(args.get("lines", -5) or -5), runtime)


async def _finish_run(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    summary = str(args.get("summary", "") or "Finished.")
    success = bool(args.get("success", True))
    failure_reason = str(args.get("failure_reason", "") or "")
    return ToolExecutionResult(
        ok=success,
        message=summary,
        payload={"summary": summary, "success": success, "failure_reason": failure_reason},
        terminal=True,
    )


def build_low_level_tools() -> list[ExperimentTool]:
    return [
        ExperimentTool(
            name="activate_app",
            description="Open or bring a macOS app to the foreground.",
            parameters={
                "type": "object",
                "properties": {"app_name": {"type": "string"}},
                "required": ["app_name"],
            },
            func=_activate_app,
        ),
        ExperimentTool(
            name="type_text",
            description="Type text into the currently focused field. If text is omitted, reuse the last typed message in this run.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
            func=_type_text,
        ),
        ExperimentTool(
            name="press_key",
            description="Press a navigation or control key such as return, tab, up, or down.",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": ["return", "tab", "space", "escape", "up", "down", "left", "right", "delete"]},
                    "times": {"type": "integer"},
                },
                "required": ["key"],
            },
            func=_press_key,
        ),
        ExperimentTool(
            name="run_shortcut",
            description="Press a keyboard shortcut such as command+f or command+shift+n.",
            parameters={
                "type": "object",
                "properties": {"keys": {"type": "string"}},
                "required": ["keys"],
            },
            func=_run_shortcut,
        ),
        ExperimentTool(
            name="click_point",
            description="Click at a specific screen coordinate.",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "click_type": {"type": "string", "enum": ["single", "double", "right"]},
                },
                "required": ["x", "y"],
            },
            func=_click_point,
        ),
        ExperimentTool(
            name="move_mouse",
            description="Move the mouse pointer to a specific coordinate.",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
            },
            func=_move_mouse,
        ),
        ExperimentTool(
            name="drag_mouse",
            description="Drag the mouse from one coordinate to another.",
            parameters={
                "type": "object",
                "properties": {
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
            func=_drag_mouse,
        ),
        ExperimentTool(
            name="scroll_view",
            description="Scroll the active view by a number of wheel lines. Negative scrolls down.",
            parameters={
                "type": "object",
                "properties": {"lines": {"type": "integer"}},
            },
            func=_scroll_view,
        ),
        ExperimentTool(
            name="finish_run",
            description="Finish the run and report whether the task succeeded.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "success": {"type": "boolean"},
                    "failure_reason": {"type": "string"},
                },
                "required": ["summary", "success"],
            },
            func=_finish_run,
        ),
    ]
