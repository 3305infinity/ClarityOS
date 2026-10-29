"""
Moonwalk — Tool Registry
=========================
Decorated Python functions that the LLM can invoke by name.
Each tool auto-registers into the global registry and exports
its schema in Gemini function_declarations format.

Every tool declaration automatically includes a `reasoning` argument
so the LLM must explain *why* it is calling the tool.  The reasoning
string is stripped before execution (tools never see it).
"""

import asyncio
import contextvars
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Any, Optional

from tools.contracts import error_envelope


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

# Default timeout for tool execution (seconds).
# Individual tools can override via the `timeout` decorator kwarg.
DEFAULT_TOOL_TIMEOUT: float = 60.0

# Maximum allowed timeout (prevents accidental infinite waits).
MAX_TOOL_TIMEOUT: float = 300.0

# Perception-style tools inherit DEFAULT_TOOL_TIMEOUT (60s), which in practice
# meant no bound at all: a single `read_screen` was measured at 39.8s inside a
# request that ran past 130s. These are reads of a screen that is changing
# anyway — a slow one is not worth the wall-clock, and a faster alternative
# route usually exists.
TOOL_TIMEOUT_OVERRIDES: dict[str, float] = {
    "read_screen": 20.0,
    "get_ui_tree": 12.0,
    "capture_region_screenshot": 12.0,
    "get_running_apps": 8.0,
    "clipboard_ops": 6.0,
}

# Tools that talk to the user rather than doing work. The task budget exists to
# bound *retrieval and actions*; applying it to these meant a task that spent
# its budget retrieving could no longer report anything, and the user saw a raw
# "task.deadline: budget exhausted before 'await_reply' could run" instead of a
# result or a readable timeout. They do no external I/O, so they always run.
COMMUNICATION_TOOLS: frozenset[str] = frozenset({
    "send_response", "await_reply",
})

# Whole-task budget shared by every tool call in one request. Without this,
# individually "reasonable" timeouts still added up to minutes.
TASK_DEADLINE_ENV = "CLARITYOS_TASK_DEADLINE_S"
DEFAULT_TASK_DEADLINE_S: float = float(os.environ.get(TASK_DEADLINE_ENV, "150"))

# Monotonic timestamp by which the current task must stop calling tools.
_task_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "clarityos_task_deadline", default=None
)


def start_task_budget(seconds: Optional[float] = None) -> float:
    """Begin a task budget. Returns the deadline (monotonic seconds)."""
    budget = float(seconds if seconds is not None else DEFAULT_TASK_DEADLINE_S)
    deadline = time.monotonic() + max(5.0, budget)
    _task_deadline.set(deadline)
    _task_failures.set({})
    return deadline


def clear_task_budget() -> None:
    _task_deadline.set(None)
    _task_failures.set({})


def task_time_remaining() -> Optional[float]:
    deadline = _task_deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def deadline_expired() -> bool:
    """True once ordinary work tools must stop being scheduled."""
    remaining = task_time_remaining()
    return remaining is not None and remaining <= 0.5


# ── Repeat-failure budget ──────────────────────────────────────────────────
# The deadline guard refuses a tool *per call*, but nothing stopped the agent
# from calling it again: an observed run issued read_screen repeatedly, then
# get_ui_tree and gworkspace_analyze, all after the budget was spent. Each
# attempt costs an LLM decision, which is what turned a 503 into a 429.
#
# Keyed on tool + arguments, and only counted when the call actually failed, so
# a legitimate repeat of a succeeding call (polling a snapshot, refreshing refs)
# is untouched.
MAX_SAME_CALL_FAILURES: int = int(os.environ.get("CLARITYOS_MAX_SAME_CALL_FAILURES", "2"))

_task_failures: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "clarityos_task_failures", default=None
)


def _signature(name: str, args: dict) -> str:
    try:
        items = sorted(
            (k, str(v)[:120]) for k, v in (args or {}).items() if k != "reasoning"
        )
    except Exception:
        items = []
    return f"{name}|{items}"


def reset_task_failures() -> None:
    _task_failures.set({})


def _failure_count(sig: str) -> int:
    book = _task_failures.get()
    return int((book or {}).get(sig, 0))


def _note_failure(sig: str) -> None:
    book = _task_failures.get()
    if book is None:
        book = {}
        _task_failures.set(book)
    book[sig] = book.get(sig, 0) + 1


def _is_failure_result(result: str) -> bool:
    text = (result or "").lstrip()
    if text[:6].upper().startswith("ERROR"):
        return True
    head = text[:400].lower()
    return "'ok': false" in head or '"ok": false' in head


# ═══════════════════════════════════════════════════════════════
#  Reasoning injection — auto-appended to every tool declaration
# ═══════════════════════════════════════════════════════════════

_REASONING_PROPERTY: dict = {
    "reasoning": {
        "type": "string",
        "description": (
            "One sentence explaining WHY you are calling this tool right now "
            "and what you expect the result to be. REQUIRED."
        ),
    }
}

# Tools that do not need a reasoning argument (communication tools)
_REASONING_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "send_response", "await_reply",
})


# ═══════════════════════════════════════════════════════════════
#  Tool Registry Infrastructure
# ═══════════════════════════════════════════════════════════════

@dataclass
class ToolDef:
    """Metadata for a registered tool."""
    name: str
    description: str
    parameters: dict          # JSON-schema style
    func: Callable            # The actual async function
    timeout: float = DEFAULT_TOOL_TIMEOUT  # Per-tool timeout in seconds
    reasoning_exempt: bool = False  # If True, no reasoning arg injected


class ToolRegistry:
    """Holds all available tools and serializes them for Gemini."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        timeout: float = DEFAULT_TOOL_TIMEOUT,
        reasoning_exempt: bool = False,
    ):
        """Decorator to register a tool function.
        
        Args:
            name: Tool name visible to the LLM.
            description: Tool description.
            parameters: JSON-schema style parameter spec.
            timeout: Max seconds the tool may run before being cancelled.
            reasoning_exempt: If True, no ``reasoning`` arg is injected.
        """
        _timeout = min(max(1.0, float(timeout)), MAX_TOOL_TIMEOUT)
        def decorator(func: Callable):
            self._tools[name] = ToolDef(
                name=name,
                description=description,
                parameters=parameters,
                func=func,
                timeout=_timeout,
                reasoning_exempt=reasoning_exempt,
            )
            return func
        return decorator

    async def execute(self, name: str, args: dict) -> str:
        """Execute a tool by name with given arguments.

        The ``reasoning`` key is silently stripped from *args* so tool
        functions never need to accept it.  Returns result string.

        Enforces a per-tool timeout to prevent hung tools from blocking
        the agent indefinitely.
        """
        tool = self._tools.get(name)
        if not tool:
            return str(error_envelope(
                code="tool.not_found",
                message=f"Unknown tool '{name}'",
            ))
        # Refuse rather than start work that cannot finish inside the task
        # budget. A tool that starts here would otherwise return after the
        # agent had already answered, and its stale result could be attributed
        # to the next request.
        remaining = task_time_remaining()
        if (remaining is not None and remaining <= 0.5
                and name not in COMMUNICATION_TOOLS):
            return str(error_envelope(
                code="task.deadline",
                message=(f"Task budget exhausted before '{name}' could run. "
                         f"Raise {TASK_DEADLINE_ENV} if this task legitimately needs longer."),
            ))

        # Refuse a call that has already failed this way. Repeating it with
        # unchanged inputs cannot produce a different answer, and each repeat
        # spends an LLM decision from the same quota the task needs to report.
        sig = _signature(name, args)
        if (name not in COMMUNICATION_TOOLS
                and _failure_count(sig) >= MAX_SAME_CALL_FAILURES):
            return str(error_envelope(
                code="tool.repeated_failure",
                message=(f"'{name}' already failed {MAX_SAME_CALL_FAILURES}x with these "
                         f"arguments. Use a different method or report what is missing; "
                         f"do not retry this call."),
            ))

        effective = min(tool.timeout, TOOL_TIMEOUT_OVERRIDES.get(name, tool.timeout))
        if remaining is not None and name not in COMMUNICATION_TOOLS:
            # Communication tools keep their own timeout: clamping them to a
            # spent budget gave wait_for a negative deadline, so delivering the
            # answer failed with tool.timeout instead of reaching the user.
            effective = min(effective, remaining)

        try:
            clean_args = {k: v for k, v in args.items() if k != "reasoning"}
            result = await asyncio.wait_for(
                tool.func(**clean_args),
                timeout=effective,
            )
            text = str(result)
            if _is_failure_result(text):
                _note_failure(sig)
            return text
        except asyncio.TimeoutError:
            _note_failure(sig)
            return str(error_envelope(
                code="tool.timeout",
                message=f"Tool '{name}' timed out after {tool.timeout:.0f}s",
                retryable=True,
            ))
        except TypeError as e:
            return str(error_envelope(
                code="tool.invalid_args",
                message=f"Invalid arguments for '{name}': {e}",
            ))
        except Exception as e:
            _note_failure(sig)
            return str(error_envelope(
                code="tool.execution_error",
                message=f"Error executing {name}: {e}",
                retryable=True,
            ))

    def declarations(self, exclude: Optional[set] = None) -> list[dict]:
        """Export tools in Gemini function_declarations format.

        Automatically injects the ``reasoning`` property into every
        non-exempt tool so the LLM is forced to justify each call.

        Args:
            exclude: Optional set of tool names to omit from the list.
                     Excluded tools remain callable via ``execute()``
                     but won't appear in the LLM's schema.
        """
        _exclude = exclude or set()
        decls = []
        for t in self._tools.values():
            if t.name in _exclude:
                continue
            params = dict(t.parameters) if t.parameters else {"type": "object", "properties": {}}
            is_exempt = t.reasoning_exempt or t.name in _REASONING_EXEMPT_TOOLS
            if not is_exempt:
                # Deep-copy properties and inject reasoning
                props = dict(params.get("properties", {}))
                props.update(_REASONING_PROPERTY)
                params = {**params, "properties": props}
                # Add reasoning to required list
                required = list(params.get("required", []))
                if "reasoning" not in required:
                    required.append("reasoning")
                params["required"] = required
            decls.append({
                "name": t.name,
                "description": t.description,
                "parameters": params,
            })
        return decls

    def list_names(self) -> list[str]:
        return list(self._tools.keys())


# ── Global registry instance ──
registry = ToolRegistry()


# ═══════════════════════════════════════════════════════════════
#  Helper: run AppleScript
# ═══════════════════════════════════════════════════════════════

async def _osascript(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        return f"AppleScript error: {err}"
    return stdout.decode("utf-8", errors="replace").strip()
