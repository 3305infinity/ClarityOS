"""
ClarityOS — topmost external window
===================================

Perception samples the foreground window at the moment a request is submitted.
By then the user has clicked into the ClarityOS sidebar to type, so the
foreground window is *ours*, and "what is on this screen" was answered with
"the active window is ClarityOS Sidebar — there is nothing to summarise" while
VS Code sat plainly visible behind it.

Carrying forward the last externally-focused app does not help: perception only
ever runs while ClarityOS has focus, so that history is never populated.

The reliable answer is the window stack itself. EnumWindows walks top-level
windows in Z-order, so the first visible, titled, non-ClarityOS window is the
one the user was looking at. Pure ctypes — no new dependency.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional, Tuple

# Process names ClarityOS itself runs under.
_OWN_PROCESSES = {"electron.exe", "clarityos.exe"}
# Title fragments that mark one of our own windows.
_OWN_TITLE_MARKERS = ("clarityos", "moonwalk")
# Shell windows that are visible but are not "an application the user is using".
_SHELL_TITLES = {
    "program manager", "windows input experience", "search",
    "start", "setup", "task view", "snap assist",
}


def _process_name(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _app_name_from_process(process: str, title: str) -> str:
    """Human-facing app name the rest of the agent already understands."""
    stem = (process or "").rsplit(".", 1)[0].lower()
    known = {
        "code": "Visual Studio Code",
        "devenv": "Visual Studio",
        "chrome": "Google Chrome",
        "msedge": "Microsoft Edge",
        "brave": "Brave Browser",
        "firefox": "Firefox",
        "explorer": "File Explorer",
        "notepad": "Notepad",
        "windowsterminal": "Windows Terminal",
        "powershell": "PowerShell",
        "cmd": "Command Prompt",
        "spotify": "Spotify",
        "slack": "Slack",
        "discord": "Discord",
    }
    if stem in known:
        return known[stem]
    # Fall back to the trailing segment of the title ("file.py - MyApp").
    if " - " in title:
        tail = title.rsplit(" - ", 1)[-1].strip()
        if tail and len(tail) <= 40:
            return tail
    return stem.title() if stem else ""


def topmost_external_window() -> Optional[Tuple[str, str]]:
    """
    (app_name, window_title) of the frontmost window that is not ClarityOS.

    Returns None on non-Windows, or when nothing suitable is visible.
    """
    if os.name != "nt":
        return None

    user32 = ctypes.windll.user32
    found: list[Tuple[str, str]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        if found:
            return False                       # stop at the first match
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = (buf.value or "").strip()
        if not title or title.lower() in _SHELL_TITLES:
            return True
        if any(marker in title.lower() for marker in _OWN_TITLE_MARKERS):
            return True                        # one of our own windows

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = _process_name(pid.value)
        if process.lower() in _OWN_PROCESSES:
            return True

        found.append((_app_name_from_process(process, title), title))
        return False

    try:
        user32.EnumWindows(WNDENUMPROC(_callback), 0)
    except Exception:
        return None

    return found[0] if found else None
