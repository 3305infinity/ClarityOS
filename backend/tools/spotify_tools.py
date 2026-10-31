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
Opens the track in the real Spotify desktop app and stops there:

  * `spotify:search:<query>` — Spotify registers this URI scheme on install, so
    the desktop client opens its own search for the term. No API, no auth.

Why it does not press Play
--------------------------
Every route to actually starting playback was tried and each one was worse
than leaving it to the user:

  * there is no Play button to click — Spotify is an Electron app and its whole
    window surfaces as a single unnamed group in UI Automation;
  * the media key does not play the requested track, it resumes whatever was
    queued before, so asking for one song started another;
  * `spotify:track:<id>` does play the right track, but the id had to be
    scraped from open.spotify.com through the browser extension: a Chrome tab
    and several seconds per song, and then it auto-played a version the user
    had not picked.

So this opens the search and says so. The user clicks the track they want.
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

# How long to let the app launch, search, and start playing.
LAUNCH_WAIT_S = 12.0


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


def installed() -> bool:
    """Is Spotify present on this PC, whether or not it is running?

    `_running()` was too strict a test for routing a playback request: "open
    Spotify and play Starboy" gets planned as a single play_media call, so
    Spotify has not been launched yet at the moment the decision is made, and
    the request fell through to a YouTube search page that plays nothing.

    The registered `spotify:` URI handler is the authoritative signal — it is
    what ensure_running() and play_track() actually use, and it covers the
    Microsoft Store build, which does not live under Program Files.
    """
    if os.name != "nt":
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "spotify"))
        return True
    except Exception:
        pass
    for path in (
        os.path.join(os.environ.get("APPDATA", ""), "Spotify", "Spotify.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "Spotify.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Spotify", "Spotify.exe"),
    ):
        if path and os.path.isfile(path):
            return True
    return False






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


# ── resolving a track id through the browser ────────────────────────────────
#
# `spotify:search:` only *shows* results; the media key then resumes whatever
# was queued before, so asking for Starboy could start something else entirely.
# `spotify:track:<id>` plays the exact track — but the id has to come from
# somewhere, and there are no Spotify API credentials configured here.
#
# open.spotify.com is readable without an account, and ClarityOS already has a
# Chrome extension that can read a page. So: open the search there, read the
# first /track/<id> link out of the snapshot, and hand that id to the desktop
# app. Costs a browser tab and a few seconds; in exchange the right track plays.


# How long to wait for the extension to publish a snapshot of the results.






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









async def play_track(query: str) -> str:
    """Open `query` in Spotify's search. Does not start playback.

    Playback used to be attempted here and it was never worth it. Spotify is an
    Electron app with no usable UI Automation tree, so there is no Play button
    to click; the media key only resumes whatever was queued before, which
    started the wrong song; and resolving a track id through the browser to use
    `spotify:track:` meant opening a Chrome tab and waiting seconds for it,
    which then auto-played a version the user had not chosen.

    So: open the search and hand it over. One URI call, no browser, no waiting,
    and the user picks the track they actually want.
    """
    if os.name != "nt":
        return "ERROR: Spotify control is only implemented for Windows."
    want = (query or "").strip()
    if not want:
        return "ERROR: no track given."

    if not await ensure_running():
        return ("ERROR: Spotify did not start. It is installed, but the app did not "
                "come up within the wait. Open Spotify once by hand and ask me again.")

    if not await open_search(want):
        return "ERROR: Spotify is running but would not accept the search request."

    return (f"Spotify is open with '{want}' in its search results - play it at "
            f"your convenience.")
