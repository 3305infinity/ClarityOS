"""
ClarityOS — deterministic Windows actions
========================================
A tiny, closed-vocabulary dispatcher for requests that need no planning at all,
such as "open notepad".

Why it exists: launching Notepad previously required a routing call, a planning
call and an executor call. When Gemini is out of quota all three fail, so a
one-word request that the machine can satisfy locally reports a planning error.

Safety properties (deliberate, please preserve when extending):

* The vocabulary is a **fixed table** — user text selects an entry, it never
  becomes one. Nothing the user types is forwarded as a command string.
* The dispatcher emits a single `open_app` call with a canonical name from that
  table, executed through the normal tool registry, so helper permissions,
  timeouts and the kill-switch all still apply.
* Anything compound ("open notepad and type…"), conditional, or merely
  app-adjacent falls through to the LLM planner untouched.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["WINDOWS_APPS", "match_direct_open", "describe_direct_open"]

# Spoken phrase -> canonical app name handed to the `open_app` tool.
# The Windows helper resolves these via its own alias table or the Start menu
# (see windows_helper/desktop.py), so names here match what Windows calls them.
WINDOWS_APPS = {
    "notepad": "Notepad",
    "note pad": "Notepad",
    "file explorer": "File Explorer",
    "windows explorer": "File Explorer",
    "explorer": "File Explorer",
    "calculator": "Calculator",
    "calc": "Calculator",
    "paint": "Paint",
    "ms paint": "Paint",
    "mspaint": "Paint",
    "wordpad": "WordPad",
    "task manager": "Task Manager",
    "command prompt": "Command Prompt",
    "cmd": "Command Prompt",
    "powershell": "PowerShell",
    "windows terminal": "Terminal",
    "terminal": "Terminal",
    "snipping tool": "Snipping Tool",
    "control panel": "Control Panel",
    "registry editor": "Registry Editor",
    "device manager": "Device Manager",
    "notepad++": "Notepad++",
    "chrome": "Chrome",
    "google chrome": "Chrome",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "firefox": "Firefox",
    "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "docker": "Docker Desktop",
    "docker desktop": "Docker Desktop",
    "spotify": "Spotify",
    "discord": "Discord",
    "slack": "Slack",
    "word": "Word",
    "excel": "Excel",
    "powerpoint": "PowerPoint",
    "outlook": "Outlook",
    "teams": "Microsoft Teams",
    "microsoft teams": "Microsoft Teams",
}

# "open notepad", "launch the calculator", "start file explorer on windows please"
_OPEN_RE = re.compile(
    r"""^\s*
        (?:please\s+)?
        (?:can\s+you\s+|could\s+you\s+)?
        (?:open|launch|start|run)\s+
        (?:up\s+)?
        (?:the\s+|my\s+)?
        (?P<app>.+?)
        (?:\s+app(?:lication)?)?
        (?:\s+(?:on|in|for)\s+(?:windows|my\s+pc|my\s+computer|the\s+desktop))?
        (?:\s+please)?
        [\s.!]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Markers of a compound or conditional request — those still need real planning.
_COMPOUND = (
    " and ", " then ", " after ", " before ", " with ", " using ", " so that ",
    " if ", " when ", " while ", ",", ";", "&&", "|",
)


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def match_direct_open(text: str) -> Optional[str]:
    """
    Canonical app name for an unambiguous "open <known app>" request.

    Returns None for anything else — compound requests, unknown apps, or
    phrasing that implies more than a launch. Callers must treat None as
    "hand this to the planner", never as a failure.
    """
    normalized = _normalize(text)
    if not normalized or len(normalized) > 80:
        return None
    # Guard before matching: "open notepad and type hello" must not launch Notepad
    # and silently drop the rest of the request.
    if any(marker in normalized for marker in _COMPOUND):
        return None

    match = _OPEN_RE.match(normalized)
    if not match:
        return None

    candidate = _normalize(match.group("app"))
    if not candidate:
        return None
    # Exact table lookup only. No fuzzy matching: guessing which app the user
    # meant is exactly the judgement call the planner exists to make.
    return WINDOWS_APPS.get(candidate)


def describe_direct_open(app_name: str) -> str:
    """Confirmation text shown once the launch succeeds."""
    return f"Opened {app_name}."
