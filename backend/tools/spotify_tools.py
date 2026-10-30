"""
ClarityOS — Spotify desktop control
===================================

Why this exists
---------------
`play_media` was hardcoded to open a YouTube *search results* page and report
"Opened YouTube search", which the agent then relayed as "Playing <track> on
YouTube". Three things were wrong with that: the user asked for Spotify, a
search page is not playback, and success was claimed without anything playing.

What this does instead
----------------------
Drives the real Spotify desktop app through mechanisms it already supports:

  * `spotify:search:<query>` — Spotify registers this URI scheme on install, so
    the desktop client opens its own search for the term. No API, no auth.
  * the existing Windows automation tools to press Play on the result.
  * the window title as the verification signal: Spotify shows "Track - Artist"
    while a track is playing and plain "Spotify"/"Spotify Free"/"Spotify
    Premium" when idle or paused. That is observable without any account access.

There is no Spotify Web API integration here, and none is assumed. If playback
cannot be confirmed from the window title, this says so rather than guessing.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import urllib.parse
from functools import partial
from typing import Optional

print = partial(print, flush=True)

SPOTIFY_IMAGE = "Spotify.exe"

# Titles Spotify shows when nothing is playing. Anything else is a track.
_IDLE_TITLE_RE = re.compile(r"^\s*spotify(\s+(free|premium))?\s*$", re.IGNORECASE)

# How long to let the app launch, search, and start playing.
LAUNCH_WAIT_S = 12.0
SEARCH_WAIT_S = 6.0
PLAY_CONFIRM_S = 8.0


def _running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {SPOTIFY_IMAGE}", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000,
        ).stdout or ""
    except Exception:
        return False
    return SPOTIFY_IMAGE.lower() in out.lower()


def window_title() -> str:
    """Spotify's main window title, or "" when it has none visible."""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {SPOTIFY_IMAGE}", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000,
        ).stdout or ""
    except Exception:
        return ""
    pids = {int(p.split()[1]) for p in out.splitlines()
            if p.strip() and p.split()[1].isdigit()}
    if not pids:
        return ""

    found: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip():
                    found.append(buf.value.strip())
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return ""
    # Prefer a title that looks like a track over the bare app name.
    for title in found:
        if not _IDLE_TITLE_RE.match(title):
            return title
    return found[0] if found else ""


def now_playing() -> Optional[str]:
    """"Track - Artist" when something is playing, else None."""
    title = window_title()
    if not title or _IDLE_TITLE_RE.match(title):
        return None
    return title


async def _wait_for(predicate, timeout_s: float, interval: float = 0.6):
    """Poll until predicate() is truthy or the timeout elapses."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    return predicate()


async def ensure_running() -> bool:
    """Launch Spotify if needed. True once its process exists."""
    if _running():
        return True
    try:
        os.startfile("spotify:")            # registered on install
    except Exception as exc:
        print(f"[Spotify] could not launch via URI: {exc.__class__.__name__}")
        return False
    return bool(await _wait_for(_running, LAUNCH_WAIT_S))


async def open_search(query: str) -> bool:
    """Show Spotify's own search results for `query` in the desktop app."""
    term = urllib.parse.quote(query.strip())
    if not term:
        return False
    try:
        os.startfile(f"spotify:search:{term}")
        return True
    except Exception as exc:
        print(f"[Spotify] search URI failed: {exc.__class__.__name__}")
        return False


# ── matching ────────────────────────────────────────────────────────────────

_NOISE = {"the", "a", "an", "by", "song", "track", "play", "on", "spotify", "feat", "ft"}


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _NOISE and len(w) > 1}


def title_matches(query: str, title: str) -> bool:
    """Is the track in the window title the one that was asked for?

    Spotify's media key resumes whatever was queued before, so "it is playing"
    and "it is playing what you asked for" are different questions. Requires
    every meaningful word of the request to appear in the title.
    """
    want, got = _tokens(query), _tokens(title)
    if not want or not got:
        return False
    return want.issubset(got)


def _media_key() -> None:
    import ctypes
    VK_MEDIA_PLAY_PAUSE = 0xB3
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 2, 0)


async def play_track(query: str) -> str:
    """Open `query` in Spotify and try to start it. Reports what really happened."""
    if os.name != "nt":
        return "ERROR: Spotify control is only implemented for Windows."
    want = (query or "").strip()
    if not want:
        return "ERROR: no track given."

    if not await ensure_running():
        return ("ERROR: I couldn't start the Spotify desktop app. Open it once "
                "manually and try again.")

    was_playing = now_playing()
    if not await open_search(want):
        return "ERROR: Spotify is running but would not accept the search request."
    await asyncio.sleep(SEARCH_WAIT_S)

    # The media key is the only playback trigger available without the Web API:
    # Spotify is an Electron app and exposes no usable UI Automation tree (its
    # whole window surfaces as a single unnamed group), so there is no Play
    # button to click.
    if not now_playing():
        _media_key()

    playing = await _wait_for(now_playing, PLAY_CONFIRM_S)

    if not playing:
        return (f"I opened '{want}' in Spotify's search, but playback did not start. "
                f"Press Play in Spotify, or tell me to try again.")

    if title_matches(want, playing):
        return f"PLAYING: {playing} (confirmed in the Spotify desktop app)"

    # Playing, but not what was asked for — the key resumed the old queue.
    # Leave the user's audio as we found it rather than silently changing it.
    if not was_playing:
        _media_key()
        await asyncio.sleep(1.0)
    return (f"Spotify is open with '{want}' searched, but the track that started was "
            f"'{playing}', not what you asked for — Spotify resumed its previous queue. "
            f"Click the track in Spotify's search results to play it.")
