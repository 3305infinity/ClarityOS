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

# Spoken phrases for the tools a user actually waits on. Keyed on tool name so
# the line says what is happening, not which function is running.
_TOOL_PHRASES = {
    "browser_read_page": "Reading the page",
    "browser_read_text": "Reading the page",
    "browser_find": "Looking for that on the page",
    "browser_snapshot": "Checking the open tab",
    "browser_list_tabs": "Checking your open tabs",
    "read_page_content": "Reading the page",
    "get_page_summary": "Reading the page",
    "read_screen": "Looking at your screen",
    "get_ui_tree": "Looking at the window",
    "capture_region_screenshot": "Taking a look at the screen",
    "read_active_editor_file": "Reading the file you have open",
    "read_file": "Reading that file",
    "web_search": "Searching the web",
    "open_url": "Opening that in your browser",
    "gmail_draft": "Drafting the reply",
    "gmail_read": "Reading your mail",
    "get_running_apps": "Checking what's open",
    "await_reply": "Waiting for you",
}

# Events that are pure internal chatter — never spoken.
_SKIP_TOOLS = {"router", "planner"}

# Strips anything that would read badly or leak internals.
_URLISH = re.compile(r"https?://\S+|www\.\S+")
_PATHISH = re.compile(r"[A-Za-z]:\\\S*|\\\\\S+")
_CODEY = re.compile(r"[{}\[\]<>|`$\"]")


def _clean(text: str, limit: int = 150) -> str:
    """One short sentence: strip what reads badly rather than dropping the line.

    Rejecting any line that contained a URL or a path meant most real progress
    was skipped — "Open https://youtube.com" and "Extract the hackathon URL
    from the active Gmail message" both vanished, which is why the assistant
    sounded mute until the final answer.
    """
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    s = _URLISH.sub("the link", s)        # say "the link", never read it aloud
    s = _PATHISH.sub("that file", s)
    s = _CODEY.sub(" ", s)                # drop stray markup, keep the words
    s = " ".join(s.split()).split(". ")[0].rstrip(" .…")
    if not s:
        return ""
    if len(s) > limit:                    # truncate at a word boundary
        s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return s


class ProgressNarrator:
    MIN_GAP_S = 2.2                   # don't narrate faster than this
    MAX_UPDATES = 12                  # per task, so long runs stay calm

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
        self._acked = False        # one spoken acknowledgement per request

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

        # The acknowledgement is the first thing the agent emits, long before
        # planning finishes. It was rendered as text only, so the user heard
        # nothing at all until the final answer — the single biggest reason the
        # assistant felt mute.
        if kind == "ack":
            if self._acked:
                return ""          # one acknowledgement per task, not two
            self._acked = True
            return _clean(event.get("text", ""))

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

        # Everything else used to fall through to silence, so a task that spent
        # thirty seconds reading a page, looking at the screen and drafting a
        # reply said nothing at all. Narrate what is actually happening, in the
        # user's terms rather than the tool's name.
        phrase = _TOOL_PHRASES.get(tool)
        if phrase:
            return phrase
        if tool.startswith("browser_"):
            return "Reading the page"
        if tool.startswith(("gmail_", "gdocs_", "gsheets_", "gcal_", "gdrive_")):
            return "Working with your Google account"
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
        # The very first line is never delayed — an acknowledgement that
        # arrives after the work has started is not an acknowledgement.
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
