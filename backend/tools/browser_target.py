"""
ClarityOS — explicit browser targeting
======================================

`open_url` called `webbrowser.open()`, which hands the URL to whatever the OS
has registered as the default browser. A user who said "open it in the Chrome
window I have here" got Edge, and the tool still reported success.

This module resolves an explicitly named browser to a real executable, checks
whether it is already running, and confirms afterwards which browser actually
took the URL. When the requested browser is unavailable the caller is expected
to say so — never to substitute a different one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional

# Canonical name -> (process image name, candidate executables)
_BROWSERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chrome": ("chrome.exe", (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    )),
    "edge": ("msedge.exe", (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    )),
    "firefox": ("firefox.exe", (
        r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
    )),
    "brave": ("brave.exe", (
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
    )),
}

# How a user names a browser. Matched against the request text, so "chromium"
# and "google chrome" both land on chrome.
_ALIASES: tuple[tuple[str, str], ...] = (
    (r"google\s*chrome|chromium|\bchrome\b", "chrome"),
    (r"microsoft\s*edge|\bedge\b", "edge"),
    (r"mozilla\s*firefox|\bfirefox\b", "firefox"),
    (r"\bbrave\b", "brave"),
)


def parse_browser_target(text: str) -> str:
    """Return the canonical browser the user explicitly named, or ""."""
    low = " ".join((text or "").lower().split())
    if not low:
        return ""
    for pattern, name in _ALIASES:
        if re.search(pattern, low):
            return name
    return ""


# "in the current window", "this tab", "here" — the user is pointing at the
# browser in front of them, which is just as explicit as naming it.
_CURRENT_WINDOW_RE = re.compile(
    r"(?i)\b(?:in|on|to)?\s*(?:the|this)\s+"
    r"(?:current|same|existing|open)?\s*(?:window|tab|browser|page)\b"
    r"|\bcurrent window\b|\bthis window\b|\bright here\b|\bover here\b"
)


def means_current_window(text: str) -> bool:
    return bool(_CURRENT_WINDOW_RE.search(" ".join((text or "").lower().split())))


def resolve_target(user_text: str, foreground_browser: str = "") -> str:
    """
    Which browser this request should open in, or "" for the system default.

    Order matters:
      1. A browser the user named wins outright.
      2. "the current window" means the browser actually in front — resolving
         that to the OS default is how "open it in the current window" ended up
         in Edge while the user was looking at Chrome.
      3. Otherwise, still prefer the foreground browser when there is one. The
         user is looking at a browser; opening their link somewhere else is
         surprising, and the default-browser fallback remains for the case
         where no browser is in front at all.
    """
    named = parse_browser_target(user_text)
    if named:
        return named
    return (foreground_browser or "").strip().lower()


def resolve_executable(name: str) -> Optional[str]:
    """Full path to the browser's executable, or None if not installed."""
    entry = _BROWSERS.get((name or "").lower())
    if entry is None:
        return None
    image, candidates = entry
    for raw in candidates:
        path = os.path.expandvars(raw)
        if "%" not in path and os.path.isfile(path):
            return path
    found = shutil.which(image)
    return found or None


def process_name(name: str) -> str:
    entry = _BROWSERS.get((name or "").lower())
    return entry[0] if entry else ""


def is_running(name: str) -> bool:
    """Is this browser already running? Chrome reuses a live instance, so a
    second launch adds a tab to the existing window rather than a new one."""
    image = process_name(name)
    if not image or os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000,
        ).stdout or ""
    except Exception:
        return False
    return image.lower() in out.lower()


def launch_url(name: str, url: str) -> tuple[bool, str]:
    """
    Open *url* in the named browser. Returns (ok, message).

    No fallback: if the requested browser cannot be found the caller must
    report that, because silently using another one is the bug this exists to
    prevent.
    """
    exe = resolve_executable(name)
    if not exe:
        return False, (f"{name.capitalize()} does not appear to be installed on this PC, "
                       f"so I did not open the link. I can open it in your default "
                       f"browser instead if you'd like.")
    reused = is_running(name)
    try:
        subprocess.Popen(
            [exe, url],
            creationflags=0x08000000 if os.name == "nt" else 0,
            close_fds=True,
        )
    except Exception as exc:
        return False, f"Could not start {name}: {exc.__class__.__name__}: {exc}"
    how = "in the existing window" if reused else "in a new window"
    return True, f"Opened {url} in {name.capitalize()} {how}."


def verify_foreground(name: str) -> tuple[bool, str]:
    """
    Did the named browser actually end up in front?

    Best-effort: reuses the window scanner already used for desktop context.
    A False here is reported, never papered over.
    """
    expected = _APP_NAMES.get((name or "").lower(), "")
    if not expected:
        return False, "unknown browser"
    try:
        from agent.window_scan import topmost_external_window
        found = topmost_external_window()
    except Exception as exc:
        return False, f"could not inspect windows ({exc.__class__.__name__})"
    if not found:
        return False, "no external window is visible"
    app_name, title = found
    if (app_name or "").strip().lower() == expected.lower():
        return True, title
    return False, f"the frontmost window belongs to {app_name or 'an unknown app'}"


# Matches agent.window_scan._app_name_from_process, which is what the rest of
# the agent already uses to name the foreground app.
_APP_NAMES: dict[str, str] = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "firefox": "Firefox",
    "brave": "Brave Browser",
}
