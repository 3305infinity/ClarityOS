"""
ClarityOS — spoken progress updates
===================================
Turns the websocket progress events the agent already emits into short spoken
lines, so a multi-step task stops being silent until the very end.

It is a *selector*, not a new event source: nothing here changes what the agent
does. It listens to the same `ws_callback` stream the UI gets and decides which
transitions are worth saying out loud.

What gets spoken
----------------
    doing/milestone      → the milestone goal ("I'm opening Spotify now.")
    doing/open_app       → the app being launched
    approval_request     → "That step needs your approval before I can continue."
    tool_blocked         → "That action is blocked by your settings."

What never gets spoken
----------------------
    tool_start/tool_result, step, raw tool output, JSON, stack traces, prompts,
    router/planner chatter ("Analyzing…", "Planning…"), and anything already
    said. The final response is spoken by the existing path, not by this.

Rules that keep it from becoming noise
--------------------------------------
* **Never interrupts.** If TTS is already playing, the update is held, and only
  the *latest* pending update is spoken when the speakers free up — several
  events arriving together collapse into one line.
* **Minimum gap** between updates, so a fast task does not narrate itself.
* **No repeats** — the same text is never spoken twice in a run.
* **Silent on cancel.** Pending updates are dropped.
* Success is never announced here; this layer only says what is *starting*.
"""

from __future__ import annotations

import re
import time
from functools import partial
from typing import Any, Awaitable, Callable, Optional

print = partial(print, flush=True)

__all__ = ["ProgressNarrator"]

# Events that are pure internal chatter — never spoken.
_SKIP_TOOLS = {"router", "planner"}

# Strips anything that would read badly or leak internals.
_CODEY = re.compile(r"[{}\[\]<>|`$]|https?://|\\\\|[A-Za-z]:\\")


def _clean(text: str, limit: int = 110) -> str:
    """One short sentence, no markup, no paths, no code."""
    s = " ".join(str(text or "").split())
    if not s or _CODEY.search(s):
        return ""
    s = s.split(". ")[0].rstrip(" .…")
    if len(s) > limit:
        return ""                     # too long to be a natural aside
    return s


class ProgressNarrator:
    MIN_GAP_S = 4.0                   # don't narrate faster than this
    MAX_UPDATES = 6                   # per task, so long runs stay calm

    def __init__(self, speak: Callable[[str], Awaitable[None]],
                 is_speaking: Callable[[], bool]):
        self._speak = speak
        self._is_speaking = is_speaking
        self.reset()

    def reset(self) -> None:
        """Call at the start of each request."""
        self._pending: Optional[str] = None
        self._spoken: set = set()
        self._last_at: float = 0.0
        self._count = 0
        self._cancelled = False

    def cancel(self) -> None:
        """Drop anything queued — the user stopped the task."""
        self._pending = None
        self._cancelled = True

    # ── event → line ────────────────────────────────────────────

    def line_for(self, event: dict) -> str:
        kind = event.get("type")

        if kind == "approval_request":
            tool = str(event.get("tool") or "that step")
            return f"{tool} needs your approval before I can continue"

        if kind == "tool_blocked":
            return "That action is blocked by your current settings"

        if kind != "doing":
            return ""

        tool = str(event.get("tool") or "")
        if tool in _SKIP_TOOLS:
            return ""

        text = _clean(event.get("text", ""))
        if not text:
            return ""

        if tool == "milestone":
            # "Milestone 2/3: open Spotify" → "open Spotify"
            text = re.sub(r"^Milestone\s+\d+\s*/\s*\d+\s*:\s*", "", text).strip()
            text = _clean(text)
            if not text:
                return ""
            return f"Now: {text[0].lower() + text[1:]}"

        if tool == "open_app":
            return text               # already phrased "Opening Notepad…"

        if tool == "executor":
            return f"Starting: {text[0].lower() + text[1:]}"

        return ""

    # ── driving ─────────────────────────────────────────────────

    async def consider(self, event: dict) -> None:
        """Feed every outgoing ws event here. Speaks at most one line."""
        if self._cancelled or self._count >= self.MAX_UPDATES:
            return

        line = self.line_for(event)
        if line and line not in self._spoken:
            self._pending = line      # newer update replaces an unspoken older one

        await self.flush()

    async def flush(self) -> None:
        """Speak the pending line if the moment is right."""
        if self._cancelled or not self._pending:
            return
        if self._is_speaking():
            return                    # never cut into a sentence already playing
        now = time.time()
        if self._last_at and now - self._last_at < self.MIN_GAP_S:
            return

        line, self._pending = self._pending, None
        self._spoken.add(line)
        self._last_at = now
        self._count += 1
        try:
            await self._speak(line + ".")
        except Exception as e:
            print(f"[Narrator] could not speak update ({e.__class__.__name__})")
