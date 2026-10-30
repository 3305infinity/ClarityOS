"""
ClarityOS — macOS GUI Tools
============================
Tools for controlling the macOS desktop: launching apps, clicking,
typing, keyboard shortcuts, screen reading, and window management.
"""

from __future__ import annotations

import asyncio
import re
import webbrowser
import urllib.parse
import os
import tempfile
import time
import base64
import hashlib
import json
from typing import Optional

try:
    import Quartz as Quartz
except ImportError:
    Quartz = None  # type: ignore

from platform_profile import describe_for_os as _describe_for_os
from providers.models import fast_model as _fast_model, powerful_model as _powerful_model
from tools.registry import registry, _osascript
from agent.world_state import IntentParser


# Well-known services that are websites, not native macOS apps
KNOWN_URLS: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "google docs": "https://docs.google.com",
    "google drive": "https://drive.google.com",
    "google maps": "https://maps.google.com",
    "google sheets": "https://sheets.google.com",
    "google slides": "https://slides.google.com",
    "github": "https://github.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "twitch": "https://www.twitch.tv",
    "tiktok": "https://www.tiktok.com",
    "whatsapp": "https://web.whatsapp.com",
    "chatgpt": "https://chat.openai.com",
    "notion": "https://www.notion.so",
    "figma": "https://www.figma.com",
    "canva": "https://www.canva.com",
    "wikipedia": "https://www.wikipedia.org",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
}


def _candidate_app_names(app_name: str) -> list[str]:
    raw = (app_name or "").strip()
    if not raw:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        cleaned = (value or "").strip()
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(cleaned)

    add(raw)
    alias = IntentParser.APP_ALIASES.get(raw.lower())
    if alias:
        add(alias)
    add(raw.title())

    normalized = raw.replace(".app", "").strip()
    if normalized and normalized != raw:
        add(normalized)
        alias = IntentParser.APP_ALIASES.get(normalized.lower())
        if alias:
            add(alias)
    return candidates


async def _match_installed_app_name(app_name: str) -> Optional[str]:
    candidates = _candidate_app_names(app_name)
    if not candidates:
        return None

    search_roots = [
        "/Applications",
        os.path.expanduser("~/Applications"),
        "/System/Applications",
    ]
    installed: dict[str, str] = {}
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                if not entry.endswith(".app"):
                    continue
                base = entry[:-4]
                installed.setdefault(base.lower(), base)
        except OSError:
            continue

    for candidate in candidates:
        exact = installed.get(candidate.lower())
        if exact:
            return exact

    for candidate in candidates:
        lowered = candidate.lower()
        for installed_lower, installed_name in installed.items():
            if lowered in installed_lower or installed_lower in lowered:
                return installed_name

    for candidate in candidates:
        try:
            proc = await asyncio.create_subprocess_exec(
                "mdfind",
                f'kMDItemKind == "Application" && kMDItemFSName == "{candidate}.app"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            hits = [line.strip() for line in stdout.decode("utf-8", "ignore").splitlines() if line.strip()]
            if hits:
                return os.path.basename(hits[0]).removesuffix(".app")
        except Exception:
            continue

    return None


def _escape_applescript_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


async def _activate_target_app(app_name: str) -> None:
    target_name = (app_name or "").strip()
    if not target_name:
        return
    resolved_name = await _match_installed_app_name(target_name)
    launch_name = resolved_name or next(iter(_candidate_app_names(target_name)), target_name)
    safe_name = _escape_applescript_string(launch_name)
    try:
        await _osascript(f'tell application "{safe_name}" to activate')
        await asyncio.sleep(0.1)
    except Exception:
        return


# ═══════════════════════════════════════════════════════════════
#  Tool Definitions — macOS GUI
# ═══════════════════════════════════════════════════════════════

# ── 1. open_app ──
@registry.register(
    name="open_app",
    description="Launch or bring a macOS application to the foreground. Use the official app name (e.g. 'Google Chrome', 'Spotify', 'Finder').",
    parameters={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Name of the application to open"
            }
        },
        "required": ["app_name"]
    }
)
async def open_app(app_name: str) -> str:
    resolved_name = await _match_installed_app_name(app_name)
    launch_name = resolved_name or next(iter(_candidate_app_names(app_name)), app_name)

    # Check if it's a well-known website AND we don't have it installed natively
    if not resolved_name:
        url = KNOWN_URLS.get(app_name.lower())
        if url:
            # Check if already open in a tab before opening a new one
            from browser.store import browser_store
            existing_tab = browser_store.find_tab_by_url(url) or browser_store.find_tab_by_domain(url)
            if existing_tab:
                from tools.browser_tools import _switch_to_chrome_tab
                switched = await _switch_to_chrome_tab(existing_tab.url)
                if switched:
                    return f"{app_name} is already open — switched to that tab."
            webbrowser.open(url)
            return f"Opened {app_name} in browser"

    # Try launching as a macOS app
    proc = await asyncio.create_subprocess_exec(
        "open", "-a", launch_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    
    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", "ignore").strip()
        return f"Couldn't find '{app_name}' as an installed app{': ' + stderr_text if stderr_text else ''}"

    # Verify it actually came to foreground — poll every 100ms, max 1.0s
    _deadline = asyncio.get_event_loop().time() + 1.0
    active = ""
    while asyncio.get_event_loop().time() < _deadline:
        await asyncio.sleep(0.1)
        active = await _osascript(
            'tell application "System Events" to get name of first process whose frontmost is true'
        )
        if active and launch_name.lower() in active.lower():
            return f"Opened {launch_name} — it's now in the foreground."
    # App launched but might not be frontmost (e.g. it was already open in background)
    if resolved_name and resolved_name.lower() != app_name.lower():
        return f"Launched {resolved_name} (matched from '{app_name}'). It may already have been open — use Cmd+Tab if it's not visible."
    return f"Launched {launch_name}. It may already have been open — use Cmd+Tab if it's not visible."


# ── 2. close_window ──
@registry.register(
    name="close_window",
    description="Close the frontmost window of the currently active application.",
    parameters={
        "type": "object",
        "properties": {},
        "required": []
    }
)
async def close_window() -> str:
    # Capture state before
    before = await _osascript(
        'tell application "System Events" to tell (first process whose frontmost is true) '
        'to get {name of it, count of windows}'
    )

    result = await _osascript(
        'tell application "System Events" to keystroke "w" using command down'
    )
    if "error" in result.lower():
        return f"Failed to close window: {result}"

    # Wait for window to close — poll every 100ms, max 0.5s
    after = before
    _deadline = asyncio.get_event_loop().time() + 0.5
    while asyncio.get_event_loop().time() < _deadline:
        await asyncio.sleep(0.1)
        after = await _osascript(
            'tell application "System Events" to tell (first process whose frontmost is true) '
            'to get {name of it, count of windows}'
        )
        if after != before:
            break

    if before != after:
        return "Closed the frontmost window."
    return (
        "Sent Cmd+W but the window may not have closed — the app might have "
        "blocked it or there's an unsaved-changes dialog. Use read_screen to verify."
    )


# ── 3. quit_app ──
@registry.register(
    name="quit_app",
    description="Quit (fully close) a running macOS application.",
    parameters={
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Name of the application to quit"
            }
        },
        "required": ["app_name"]
    }
)
async def quit_app(app_name: str) -> str:
    # Step 1: Check if the app is actually running before trying to quit
    running_check = await _osascript(
        'tell application "System Events" to get name of every process whose background only is false'
    )
    running_apps = [a.strip() for a in running_check.split(",")] if running_check else []
    # Fuzzy match: "chrome" → "Google Chrome"
    matched_name = None
    name_lower = app_name.lower()
    for app in running_apps:
        if name_lower == app.lower() or name_lower in app.lower():
            matched_name = app
            break

    if not matched_name:
        return f"{app_name} is not running."

    # Step 2: Try graceful quit via AppleScript
    result = await _osascript(f'tell application "{matched_name}" to quit')
    if "error" in result.lower() and "not running" not in result.lower():
        # Step 3: Fallback — force kill via killall
        proc = await asyncio.create_subprocess_exec(
            "killall", matched_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)

    # Step 4: Poll for quit — check every 150ms, max 1.0s
    still_running = True
    _deadline = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < _deadline:
        await asyncio.sleep(0.15)
        verify = await _osascript(
            f'tell application "System Events" to (name of every process whose name is "{matched_name}")'
        )
        still_running = matched_name.lower() in verify.lower() if verify else False
        if not still_running:
            break

    if still_running:
        # Step 5: Force kill as last resort
        proc = await asyncio.create_subprocess_exec(
            "killall", "-9", matched_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
        # Poll for force-kill — every 100ms, max 0.5s
        _still = True
        _deadline2 = asyncio.get_event_loop().time() + 0.5
        while asyncio.get_event_loop().time() < _deadline2:
            await asyncio.sleep(0.1)
            verify2 = await _osascript(
                f'tell application "System Events" to (name of every process whose name is "{matched_name}")'
            )
            _still = matched_name.lower() in verify2.lower() if verify2 else False
            if not _still:
                break
        if _still:
            return f"Failed to quit {matched_name} — it refused to close even after force-kill."
        return f"Force-quit {matched_name} (it didn't respond to the normal quit)."

    return f"Quit {matched_name}."


_SPOTIFY_WORD = re.compile(r"(?i)\b(?:on|in|using|via|through)?\s*spotify\b")


def _mentions_spotify(text: str) -> bool:
    return bool(re.search(r"(?i)\bspotify\b", text or ""))


# ── 4. play_media ──
@registry.register(
    name="play_media",
    description=("Play music. If the request mentions Spotify, this drives the Spotify "
                 "desktop app and confirms what actually started playing. Otherwise it "
                 "opens YouTube search results (which does NOT start playback)."),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for and play"
            }
        },
        "required": ["query"]
    }
)
async def play_media(query: str) -> str:
    # A YouTube *search page* is not playback, and it is certainly not Spotify.
    # This used to open one unconditionally and return "Opened YouTube search",
    # which the agent relayed as "Playing <track> on YouTube" — wrong app, and
    # a success claim for something that never played.
    if _mentions_spotify(query):
        from tools.spotify_tools import play_track
        cleaned = _SPOTIFY_WORD.sub(" ", query).strip() or query
        return await play_track(cleaned)

    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.youtube.com/results?search_query={encoded}"
    webbrowser.open(url)
    # Say what actually happened: results are on screen, nothing is playing yet.
    return (f"Opened YouTube search results for '{query}'. Nothing is playing yet — "
            f"pick a video, or ask me to play it in Spotify instead.")


# ── 5. web_search ──
@registry.register(
    name="web_search",
    description="Search the web using the default browser. Useful for research, homework help, finding information.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query"
            }
        },
        "required": ["query"]
    }
)
async def web_search(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}"
    webbrowser.open(url)
    return f"Opened web search for '{query}'"


# ── 6. type_text ──
@registry.register(
    name="type_text",
    description="Type text into the currently focused input field or text area on the user's screen.",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to type"
            }
        },
        "required": ["text"]
    }
)
async def type_text(text: str) -> str:
    if not text:
        return "No text provided to type."

    # For long text (>50 chars), use clipboard paste instead of keystroke
    # — keystroke is slow and unreliable for long strings
    if len(text) > 50:
        # Save to clipboard and paste
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(input=text.encode("utf-8")), timeout=3.0)
        result = await _osascript(
            'tell application "System Events" to keystroke "v" using command down'
        )
        if "error" in result.lower():
            return f"Failed to paste text: {result}"
        return f"Pasted {len(text)} characters into the active field."

    # For short text, use keystroke
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    result = await _osascript(
        f'tell application "System Events" to keystroke "{escaped}"'
    )
    if "error" in result.lower():
        return f"Failed to type text: {result}"
    return f"Typed {len(text)} characters into the active field."


# ── 7. run_shortcut ──
@registry.register(
    name="run_shortcut",
    description="Press a keyboard shortcut. Use modifier names: 'command', 'shift', 'option', 'control'. Example: 'command+c' for copy, 'command+v' for paste. IMPORTANT: This only sends the keypress — it cannot guarantee the app acted on it. If the result matters (e.g. opening a compose window, triggering a menu), always call read_screen afterwards to verify the expected change happened. If nothing changed, try clicking the UI element directly instead.",
    parameters={
        "type": "object",
        "properties": {
            "keys": {
                "type": "string",
                "description": "The shortcut to press, e.g. 'command+c', 'command+shift+n'"
            }
        },
        "required": ["keys"]
    }
)
async def run_shortcut(keys: str) -> str:
    parts = [p.strip().lower() for p in keys.split("+")]
    key_char = parts[-1]
    modifiers = parts[:-1]

    modifier_map = {
        "command": "command down",
        "cmd": "command down",
        "shift": "shift down",
        "option": "option down",
        "alt": "option down",
        "control": "control down",
        "ctrl": "control down",
    }

    using_parts = []
    for mod in modifiers:
        mapped = modifier_map.get(mod)
        if mapped:
            using_parts.append(mapped)

    using_clause = ""
    if using_parts:
        using_clause = " using {" + ", ".join(using_parts) + "}"

    # ── Capture stable state BEFORE the keypress ──
    async def _screen_hash() -> Optional[str]:
        try:
            tmp = os.path.join(tempfile.gettempdir(), f"mw_sc_{int(time.time()*1000)}.png")
            p = await asyncio.create_subprocess_exec(
                "screencapture", "-x", "-t", "png", tmp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(p.communicate(), timeout=3.0)
            if os.path.exists(tmp):
                with open(tmp, "rb") as f:
                    h = hashlib.md5(f.read()).hexdigest()
                os.remove(tmp)
                return h
        except Exception:
            pass
        return None

    # ── Snapshot layer-0 windows (real app windows, not overlays) ──
    def _window_ids() -> set:
        try:
            if not Quartz:
                return set()
            wins = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            return {w.get("kCGWindowNumber") for w in (wins or []) if w.get("kCGWindowLayer", 999) == 0}
        except Exception:
            return set()

    before_hash = await _screen_hash()
    before_wins = _window_ids()

    script = f'tell application "System Events" to keystroke "{key_char}"{using_clause}'
    result = await _osascript(script)
    if "error" in result.lower():
        return f"Failed to run shortcut: {result}"

    # ── Wait for overlay to fade — poll every 200ms, max 1.5s ──
    # macOS shortcut overlay lasts ~0.5-1s. We poll for persistent changes.
    after_hash = before_hash
    after_wins = before_wins
    _deadline = asyncio.get_event_loop().time() + 1.5
    _settled_count = 0
    _prev_check_hash = None
    while asyncio.get_event_loop().time() < _deadline:
        await asyncio.sleep(0.2)
        after_hash = await _screen_hash()
        after_wins = _window_ids()
        # If new window appeared, exit immediately
        if after_wins - before_wins:
            break
        # If screen changed and stayed changed for 2 consecutive checks → settled
        if after_hash and after_hash != before_hash:
            if after_hash == _prev_check_hash:
                _settled_count += 1
                if _settled_count >= 1:
                    break
            else:
                _settled_count = 0
        _prev_check_hash = after_hash

    # New layer-0 window? Definitive success (new dialog/modal/window opened)
    new_wins = after_wins - before_wins
    if new_wins:
        return f"Pressed {keys} — a new window appeared, shortcut worked."

    # Screen changed persistently? Likely worked (in-page change like a compose area)
    if before_hash and after_hash and before_hash != after_hash:
        return f"Pressed {keys} — screen changed persistently, shortcut appears to have worked."

    # Screen returned to original state — only a transient overlay appeared
    return (
        f"Keystroke '{keys}' was sent but the screen returned to its original state after "
        f"the system overlay faded — the app likely ignored or blocked this shortcut. "
        f"Try using read_screen to find the button and click_element it directly instead."
    )


# ── 8. open_url ──

# Fix 4: Cooldown tracker — block rapid duplicate opens without blocking distinct searches.
_recent_url_opens: dict = {}  # cooldown_key -> timestamp
_OPEN_URL_COOLDOWN = 30  # seconds

@registry.register(
    name="open_url",
    description="Open a URL in the browser. Automatically checks if the URL is already open in an existing tab and switches to it instead of creating a duplicate. Only opens a new tab when necessary.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to open"
            },
            "force_new_tab": {
                "type": "boolean",
                "description": "Force opening in a new tab even if already open (default false)"
            },
            "browser": {
                "type": "string",
                "description": (
                    "Browser the user explicitly asked for: 'chrome', 'edge', "
                    "'firefox' or 'brave'. Leave empty when the user did not name "
                    "one, which opens the system default. Never guess a value here."
                )
            }
        },
        "required": ["url"]
    }
)
async def open_url(url: str, force_new_tab: bool = False, browser: str = "") -> str:
    import time as _time
    from urllib.parse import urlparse
    from browser.store import browser_store

    # ── Fix 4: Cooldown key ──
    try:
        parsed_url = urlparse(url.lower())
        domain = parsed_url.netloc.replace("www.", "")
        path = parsed_url.path or "/"
        query = parsed_url.query or ""
    except Exception:
        domain = ""
        path = "/"
        query = ""
    cooldown_key = domain
    if domain and (query or path not in ("", "/")):
        cooldown_key = f"{domain}{path}"
        if query:
            cooldown_key = f"{cooldown_key}?{query}"

    if cooldown_key and not force_new_tab:
        last_open = _recent_url_opens.get(cooldown_key)
        if last_open and (_time.time() - last_open) < _OPEN_URL_COOLDOWN:
            elapsed = _time.time() - last_open
            return (
                f"Already opened {cooldown_key} {elapsed:.0f}s ago. "
                f"Use browser_read_page() to interact with the page, "
                f"or browser_switch_tab(url='{url}') to switch to it."
            )

    if not force_new_tab:
        # Check if the URL (or its domain) is already open in a tab
        existing_tab = browser_store.find_tab_by_url(url)
        if existing_tab:
            # Try to switch to the existing tab
            from tools.browser_tools import _switch_to_chrome_tab
            switched = await _switch_to_chrome_tab(existing_tab.url)
            if switched:
                # Record in cooldown tracker
                if cooldown_key:
                    _recent_url_opens[cooldown_key] = _time.time()
                return f"Tab already open — switched to '{existing_tab.title or existing_tab.url}' instead of opening a duplicate."

    # An explicitly named browser is honoured exactly, or refused. webbrowser
    # .open() hands the URL to the OS default, which is how "open it in Chrome"
    # ended up in Edge while the tool still reported success.
    target = (browser or "").strip().lower()
    if target:
        from tools.browser_target import launch_url, verify_foreground
        ok, message = launch_url(target, url)
        if not ok:
            return f"ERROR: {message}"
        if cooldown_key:
            _recent_url_opens[cooldown_key] = _time.time()
        import asyncio as _asyncio
        await _asyncio.sleep(1.2)          # let the window come forward
        confirmed, detail = verify_foreground(target)
        if confirmed:
            return f"{message} Confirmed in front: {detail[:120]}"
        # Report what actually happened rather than asserting success.
        return (f"{message} I could not confirm it came to the foreground "
                f"({detail}).")

    webbrowser.open(url)
    # Record in cooldown tracker
    if cooldown_key:
        _recent_url_opens[cooldown_key] = _time.time()
    return f"Opened {url}"


# ── 9. get_running_apps ──
@registry.register(
    name="get_running_apps",
    # Platform-aware: on Windows this call is proxied to the Windows helper
    # (see hub/desktop_proxy.py PROXIED_TOOLS), but the *description* is what
    # the planner reads — saying "on the Mac" here is what pushed it towards
    # osascript-through-run_shell on Windows installs.
    description=_describe_for_os(
        "List all currently running applications on this Mac. "
        "Prefer this over run_shell — it needs no approval.",
        "List all currently running applications on this Windows PC. "
        "Prefer this over run_shell — it needs no approval and no PowerShell.",
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": []
    }
)
async def get_running_apps() -> str:
    result = await _osascript(
        'tell application "System Events" to get name of every process whose background only is false'
    )
    return f"Running apps: {result}"


# ── 10. set_volume ──
@registry.register(
    name="set_volume",
    description="Set the system volume level (0-100).",
    parameters={
        "type": "object",
        "properties": {
            "level": {
                "type": "integer",
                "description": "Volume level from 0 (mute) to 100 (max)"
            }
        },
        "required": ["level"]
    }
)
async def set_volume(level: int) -> str:
    level = max(0, min(100, level))
    result = await _osascript(f"set volume output volume {level}")
    if "error" in result.lower():
        return f"Failed to set volume: {result}"
    # Verify
    verify = await _osascript("output volume of (get volume settings)")
    try:
        actual = int(verify.strip())
        if abs(actual - level) <= 5:  # within tolerance
            return f"Volume set to {actual}%."
        return f"Set volume to {level}% but system reports {actual}%."
    except (ValueError, AttributeError):
        return f"Set volume to {level}%."


# ── 11. send_response (final answer to user) ──
@registry.register(
    name="send_response",
    description=(
        "Send a FINAL response to the user. The conversation ends after this. "
        "Choose the best `modal` type for the content:\n"
        "- **text** (default): short conversational answer, 1-3 sentences.\n"
        "- **rich**: long-form content — essays, reports, detailed explanations. "
        "  Supports a `title` and full markdown.\n"
        "- **table**: structured data — comparisons, spreadsheets, stats. "
        "  Provide `headers` (list of column names) and `rows` (list of row-lists).\n"
        "- **list**: multiple items — search results, recommendations, options. "
        "  Provide `items` (list of {title, description, icon?}).\n"
        "- **confirm**: ask the user to pick an action. "
        "  Provide `actions` (list of {label, value} button definitions).\n"
        "- **media**: show an image with optional caption. "
        "  Provide `media_url` (base64 data-uri or https URL) and optional `caption`.\n"
        "- **steps**: summarise a multi-step task you completed. "
        "  Provide `steps` (list of {label, status: done|current|pending, detail?}).\n"
        "- **plan**: present a plan to the user BEFORE executing. Uses `await_reply` internally. "
        "  Provide `steps` (list of {label, detail?}). The user can approve, modify, or cancel.\n"
        "- **cards**: display image+text cards for products, homes, articles, and visual results. "
        "  Provide `cards` (list of {name/title, description?, image?, price?, rating?, reviews?, original_price?, source?, url/link?, link_label?}). "
        "  Optionally add `title`, `subtitle`, and `context` {title, summary, highlights: [{icon, text}]}. "
        "  Legacy `modal='products'` with `products` is still accepted for backward compatibility."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The text content to display (markdown supported)"
            },
            "modal": {
                "type": "string",
                "enum": ["text", "rich", "table", "list", "confirm", "media", "steps", "plan", "cards", "products"],
                "description": "The modal layout to use (default: 'text')"
            },
            "title": {
                "type": "string",
                "description": "Title for rich/table/list/steps/plan/cards modals"
            },
            "subtitle": {
                "type": "string",
                "description": "Optional subtitle for cards/products modals"
            },
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Column headers for table modal"
            },
            "rows": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "Data rows for table modal"
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "icon": {"type": "string"}
                    },
                    "required": ["title"]
                },
                "description": "Items for list modal"
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "string"}
                    },
                    "required": ["label", "value"]
                },
                "description": "Buttons for confirm modal"
            },
            "media_url": {
                "type": "string",
                "description": "Image URL or base64 data-uri for media modal"
            },
            "caption": {
                "type": "string",
                "description": "Caption for media modal"
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "status": {"type": "string", "enum": ["done", "current", "pending"]},
                        "detail": {"type": "string", "description": "Optional short detail or tool name"}
                    },
                    "required": ["label"]
                },
                "description": "Step items for steps/plan modal. For plan modal, status is ignored (steps are shown as numbered items)."
            },
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "price": {"type": "string"},
                        "image": {"type": "string"},
                        "rating": {"type": "number"},
                        "reviews": {"type": "string"},
                        "original_price": {"type": "string"},
                        "source": {"type": "string"},
                        "url": {"type": "string"},
                        "link": {"type": "string"},
                        "link_label": {"type": "string"},
                        "description": {"type": "string", "description": "Short card description (1-2 lines)"}
                    },
                    "anyOf": [
                        {"required": ["name"]},
                        {"required": ["title"]}
                    ]
                },
                "description": "Card items for cards modal. Preferred over legacy `products`."
            },
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "price": {"type": "string"},
                        "image": {"type": "string"},
                        "rating": {"type": "number"},
                        "reviews": {"type": "string"},
                        "original_price": {"type": "string"},
                        "source": {"type": "string"},
                        "url": {"type": "string"},
                        "description": {"type": "string", "description": "Short product description (1-2 lines)"}
                    },
                    "required": ["name"]
                },
                "description": "Legacy alias for cards modal data. Accepted for backward compatibility."
            },
            "context": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Sidebar heading (e.g. 'Search Summary')"},
                    "summary": {"type": "string", "description": "Brief analysis or context about the results (markdown)"},
                    "highlights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "icon": {"type": "string", "description": "Emoji icon"},
                                "text": {"type": "string"}
                            },
                            "required": ["text"]
                        },
                        "description": "Key facts or tips as bullet highlights"
                    }
                },
                "description": "Optional sidebar with context/analysis about the product results"
            },
            "plan_id": {
                "type": "string",
                "description": "Optional plan identifier for plan/correlation flows."
            },
            "modals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "modal": {"type": "string", "enum": ["text", "rich", "table", "list", "confirm", "media", "steps", "plan", "cards", "products"]},
                        "message": {"type": "string"},
                        "title": {"type": "string"},
                        "subtitle": {"type": "string"},
                        "headers": {"type": "array", "items": {"type": "string"}},
                        "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                        "items": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "icon": {"type": "string"}}, "required": ["title"]}},
                        "actions": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label", "value"]}},
                        "media_url": {"type": "string"},
                        "caption": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "status": {"type": "string"}, "detail": {"type": "string"}}, "required": ["label"]}},
                        "cards": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "title": {"type": "string"}, "price": {"type": "string"}, "image": {"type": "string"}, "rating": {"type": "number"}, "url": {"type": "string"}, "link": {"type": "string"}, "link_label": {"type": "string"}, "description": {"type": "string"}}, "anyOf": [{"required": ["name"]}, {"required": ["title"]}]}},
                        "products": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "string"}, "image": {"type": "string"}, "rating": {"type": "number"}, "url": {"type": "string"}, "description": {"type": "string"}}, "required": ["name"]}}
                    },
                    "required": ["modal", "message"]
                },
                "description": "Array of modal definitions for multi-modal stacking. Each entry is rendered as a separate bubble stacked vertically. When provided, the top-level modal/title/etc. fields are ignored."
            }
        },
        "required": ["message"]
    }
)
async def send_response(
    message: str = "",
    modal: str = "text",
    title: str = "",
    subtitle: str = "",
    headers: list = None,
    rows: list = None,
    items: list = None,
    actions: list = None,
    media_url: str = "",
    caption: str = "",
    steps: list = None,
    cards: list = None,
    products: list = None,
    context: dict = None,
    plan_id: str = "",
    modals: list = None,
    response_text: str = "",
) -> str:
    import json as _json
    # Backward compatibility: older planners used response_text instead of message.
    if not message and response_text:
        print("[Tools] send_response: deprecated arg 'response_text' used; prefer 'message'.")
        message = response_text
    if not message:
        message = ""
    # Multi-modal: array of stacked modals
    if modals:
        payload = {"modals": modals, "message": message}
        return f"RESPONSE:{_json.dumps(payload)}"
    payload = {"modal": modal, "message": message}
    if plan_id:
        payload["plan_id"] = plan_id
    if title:
        payload["title"] = title
    if subtitle:
        payload["subtitle"] = subtitle
    if headers:
        payload["headers"] = headers
    if rows:
        payload["rows"] = rows
    if items:
        payload["items"] = items
    if actions:
        payload["actions"] = actions
    if media_url:
        payload["media_url"] = media_url
    if caption:
        payload["caption"] = caption
    if steps:
        payload["steps"] = steps
    normalized_cards = cards or products
    if normalized_cards:
        payload["cards"] = normalized_cards
    if products:
        payload["products"] = products
    if context:
        payload["context"] = context
    return f"RESPONSE:{_json.dumps(payload)}"


# ── 12. await_reply (ask user and wait for their response) ──
@registry.register(
    name="await_reply",
    description=(
        "Send a message to the user AND wait for their spoken response. "
        "Use this for interactive conversations: asking questions, joke setups, "
        "clarifying questions, or any time you need the user to respond before continuing. "
        "Supports the same modal types as send_response including multi-modal stacking via `modals`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message to show while waiting for the user's reply"
            },
            "modal": {
                "type": "string",
                "enum": ["text", "rich", "table", "list", "confirm", "media", "steps", "plan", "cards", "products"],
                "description": "The modal layout to use (default: 'text')"
            },
            "title": {"type": "string", "description": "Title for the modal"},
            "subtitle": {"type": "string", "description": "Optional subtitle for cards/products modal"},
            "headers": {"type": "array", "items": {"type": "string"}, "description": "Column headers for table"},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "Rows for table"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "icon": {"type": "string"}},
                    "required": ["title"]
                },
                "description": "Items for list modal"
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["label", "value"]
                },
                "description": "Buttons for confirm modal"
            },
            "media_url": {"type": "string", "description": "Image URL for media modal"},
            "caption": {"type": "string", "description": "Caption for media modal"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "status": {"type": "string", "enum": ["done", "current", "pending"]},
                        "detail": {"type": "string"}
                    },
                    "required": ["label"]
                },
                "description": "Steps for steps/plan modal"
            },
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "price": {"type": "string"},
                        "image": {"type": "string"},
                        "rating": {"type": "number"},
                        "reviews": {"type": "string"},
                        "original_price": {"type": "string"},
                        "source": {"type": "string"},
                        "url": {"type": "string"},
                        "link": {"type": "string"},
                        "link_label": {"type": "string"},
                        "description": {"type": "string"}
                    },
                    "anyOf": [
                        {"required": ["name"]},
                        {"required": ["title"]}
                    ]
                },
                "description": "Card items for cards modal. Preferred over legacy `products`."
            },
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "price": {"type": "string"},
                        "image": {"type": "string"},
                        "rating": {"type": "number"},
                        "reviews": {"type": "string"},
                        "original_price": {"type": "string"},
                        "source": {"type": "string"},
                        "url": {"type": "string"},
                        "description": {"type": "string"}
                    },
                    "required": ["name"]
                },
                "description": "Legacy alias for cards modal data."
            },
            "context": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "highlights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"icon": {"type": "string"}, "text": {"type": "string"}},
                            "required": ["text"]
                        }
                    }
                },
                "description": "Optional sidebar context panel for products"
            },
            "plan_id": {"type": "string", "description": "Optional plan identifier for plan/correlation flows."},
            "modals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "modal": {"type": "string", "enum": ["text", "rich", "table", "list", "confirm", "media", "steps", "plan", "cards", "products"]},
                        "message": {"type": "string"},
                        "title": {"type": "string"},
                        "subtitle": {"type": "string"},
                        "headers": {"type": "array", "items": {"type": "string"}},
                        "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                        "items": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "icon": {"type": "string"}}, "required": ["title"]}},
                        "actions": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label", "value"]}},
                        "media_url": {"type": "string"},
                        "caption": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "status": {"type": "string"}, "detail": {"type": "string"}}, "required": ["label"]}},
                        "cards": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "title": {"type": "string"}, "price": {"type": "string"}, "image": {"type": "string"}, "rating": {"type": "number"}, "url": {"type": "string"}, "link": {"type": "string"}, "link_label": {"type": "string"}, "description": {"type": "string"}}, "anyOf": [{"required": ["name"]}, {"required": ["title"]}]}},
                        "products": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "string"}, "image": {"type": "string"}, "rating": {"type": "number"}, "url": {"type": "string"}, "description": {"type": "string"}}, "required": ["name"]}}
                    },
                    "required": ["modal", "message"]
                },
                "description": "Array of modal definitions for multi-modal stacking."
            }
        },
        "required": ["message"]
    }
)
async def await_reply(
    message: str = "",
    modal: str = "text",
    title: str = "",
    subtitle: str = "",
    headers: list = None,
    rows: list = None,
    items: list = None,
    actions: list = None,
    media_url: str = "",
    caption: str = "",
    steps: list = None,
    cards: list = None,
    products: list = None,
    context: dict = None,
    plan_id: str = "",
    modals: list = None,
    prompt: str = "",
) -> str:
    import json as _json
    # Backward compatibility: older planners used prompt instead of message.
    if not message and prompt:
        print("[Tools] await_reply: deprecated arg 'prompt' used; prefer 'message'.")
        message = prompt
    if not message:
        message = ""
    # Multi-modal: array of stacked modals
    if modals:
        payload = {"modals": modals, "message": message}
        return f"AWAIT:{_json.dumps(payload)}"
    payload = {"modal": modal, "message": message}
    if plan_id:
        payload["plan_id"] = plan_id
    if title:
        payload["title"] = title
    if subtitle:
        payload["subtitle"] = subtitle
    if headers:
        payload["headers"] = headers
    if rows:
        payload["rows"] = rows
    if items:
        payload["items"] = items
    if actions:
        payload["actions"] = actions
    if media_url:
        payload["media_url"] = media_url
    if caption:
        payload["caption"] = caption
    if steps:
        payload["steps"] = steps
    normalized_cards = cards or products
    if normalized_cards:
        payload["cards"] = normalized_cards
    if products:
        payload["products"] = products
    if context:
        payload["context"] = context
    return f"AWAIT:{_json.dumps(payload)}"


# ── 12b. wait (pause execution briefly) ──
@registry.register(
    name="wait",
    description="Pause for a specified number of seconds. Use this when you need to wait for a dialog to appear after clicking a button, for an app to finish loading, or between UI interactions that need time to settle. E.g. after clicking 'Import', wait 1 second before pressing keyboard shortcuts.",
    parameters={
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "Number of seconds to wait (0.5 to 5, default 1)"
            }
        },
        "required": []
    }
)
async def wait(seconds: float = 1.0) -> str:
    seconds = max(0.2, min(5.0, seconds))
    await asyncio.sleep(seconds)
    
    # After waiting, fetch the minimal context to return to the model
    try:
        from agent.perception import get_active_app, get_window_title
        app_name, window_title = await asyncio.gather(
            get_active_app(),
            get_window_title()
        )
        return f"Waited {seconds}s. Currently active app: '{app_name}', window: '{window_title}'"
    except Exception:
        return f"Waited {seconds}s"


# ── 13. run_shell (execute terminal commands) ──

import re as _re

# Safety blocklist — reject commands containing these patterns.
# Uses both exact substring matches and regex patterns for robustness.
SHELL_BLOCKLIST = [
    "rm -rf /", "rm -rf ~", "rm -rf /*",
    "sudo rm", "sudo shutdown", "sudo reboot", "sudo halt",
    "shutdown", "reboot", "halt",
    "mkfs", "dd if=", ":(){ :|:& };:",
    "mv / ", "chmod -R 777 /",
    "> /dev/sda", "fork bomb",
]

# Regex patterns for more robust detection (case-insensitive)
SHELL_BLOCKLIST_RE = [
    _re.compile(r"\brm\s+(-\w+\s+)*(/|~|\$HOME)\b", _re.IGNORECASE),           # rm with root/home
    _re.compile(r"\bsudo\s+(rm|shutdown|reboot|halt|mkfs|dd)\b", _re.IGNORECASE), # sudo + destructive
    _re.compile(r"\b(format|fdisk|diskutil\s+erase)\b", _re.IGNORECASE),          # disk formatting
    _re.compile(r">\s*/dev/(sd|disk|nvme)", _re.IGNORECASE),                       # overwrite device
    _re.compile(r"\bcurl\b.*\|\s*(ba)?sh", _re.IGNORECASE),                       # curl pipe to shell
    _re.compile(r"\bwget\b.*\|\s*(ba)?sh", _re.IGNORECASE),                       # wget pipe to shell
    _re.compile(r"\blaunchctl\s+(unload|remove)\b", _re.IGNORECASE),              # system service removal
    _re.compile(r"\bdefaults\s+delete\b", _re.IGNORECASE),                        # macOS defaults nuke
]

# Paths that read_file/write_file should never access
RESTRICTED_PATH_PREFIXES = [
    os.path.expanduser("~/.ssh"),
    os.path.expanduser("~/.gnupg"),
    os.path.expanduser("~/.aws"),
    os.path.expanduser("~/.kube"),
    "/etc/shadow",
    "/etc/sudoers",
    "/System",
    "/usr/local/bin",
    "/private/var",
]

@registry.register(
    name="run_shell",
    description=_describe_for_os(
        "Execute a shell command in the macOS terminal (zsh/bash) and return its output. "
        "LAST RESORT — the user must approve every shell command, so use a purpose-built tool "
        "when one exists (e.g. get_running_apps, open_app). Use for: disk space, listing files, "
        "installing packages (pip/brew), scripts, git, file management. Truncated to 2000 chars.",
        "Execute a shell command in Windows PowerShell and return its output. "
        "LAST RESORT — the user must approve every shell command, so use a purpose-built tool "
        "when one exists (e.g. get_running_apps, open_app). Do NOT use AppleScript or osascript; "
        "they do not exist on Windows. Truncated to 2000 chars.",
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute (e.g. 'ls -la ~/Desktop', 'df -h', 'python3 script.py')"
            }
        },
        "required": ["command"]
    }
)
async def run_shell(command: str) -> str:
    # Safety check — substring blocklist
    cmd_lower = command.lower().strip()
    for blocked in SHELL_BLOCKLIST:
        if blocked in cmd_lower:
            return f"BLOCKED: Command contains dangerous pattern '{blocked}'. Refusing to execute."

    # Safety check — regex blocklist (catches obfuscated variants)
    for pattern in SHELL_BLOCKLIST_RE:
        match = pattern.search(command)
        if match:
            return f"BLOCKED: Command matches dangerous pattern. Refusing to execute."

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.expanduser("~"),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        
        output = stdout.decode("utf-8", errors="replace").strip()
        errors = stderr.decode("utf-8", errors="replace").strip()
        
        result = ""
        if output:
            result += output[:2000]
        if errors:
            result += f"\n[STDERR]: {errors[:500]}"
        if not result:
            result = f"Command completed (exit code {proc.returncode})"
            
        return result
    except asyncio.TimeoutError:
        return "ERROR: Command timed out after 30 seconds"
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


# ── 16. read_screen (Gemini Vision OCR) ──

def _draw_grid_overlay(filepath: str, step: int = 100) -> None:
    """Draw a subtle coordinate grid on a screenshot to help Vision models
    locate UI elements with pixel-accurate coordinates.

    Draws light gray lines every `step` pixels and labels them with their
    coordinate value.  Modifies the image file in-place.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return  # PIL not available — skip silently

    img = Image.open(filepath).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = img.size

    # Use a small built-in font
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 11)
    except Exception:
        font = ImageFont.load_default()

    line_color = (255, 50, 50, 60)   # very faint red
    text_color = (255, 50, 50, 140)  # slightly more visible for labels

    # Vertical lines + X labels at top
    for x in range(step, w, step):
        draw.line([(x, 0), (x, h)], fill=line_color, width=1)
        draw.text((x + 2, 2), str(x), fill=text_color, font=font)

    # Horizontal lines + Y labels on left
    for y in range(step, h, step):
        draw.line([(0, y), (w, y)], fill=line_color, width=1)
        draw.text((2, y + 2), str(y), fill=text_color, font=font)

    result = Image.alpha_composite(img, overlay).convert("RGB")
    result.save(filepath)


# Screen cache: always capture fresh, but skip Vision API if pixels haven't changed
_screen_cache = {"hash": None, "result": None}
# Display origin: offset of the captured display in CGEvent's unified coordinate space.
# click_element adds this to image-relative coordinates so clicks land correctly
# on multi-monitor setups where secondary displays have non-zero origins.
_display_origin = {"x": 0, "y": 0}

# ── Accessibility timeout circuit breaker ──
# Apps whose UI tree dump consistently times out (WhatsApp, Electron apps).
# Skip re-querying them so we fail-fast into the visual fallback instead
# of wasting 8-16 s on AppleScript timeouts.
_ui_tree_timeout_apps: dict = {}  # {app_lower: timestamp_of_last_timeout}
_UI_TREE_TIMEOUT_COOLDOWN = 60.0  # seconds before retrying a timed-out app


async def _fast_visual_locate(
    description: str,
    hint: str = "",
) -> Optional[tuple]:
    """Find a UI element visually using Gemini Flash (~3-5 s).

    Captures a screenshot, sends it to the *Flash* model with a focused
    prompt asking only for the element's (x, y) centre coordinates.

    Returns
    -------
    (x, y) pixel coordinates usable with `click_element`, or None.

    Cost: ~860 tokens (image + short prompt) at Flash pricing — negligible.
    """
    global _display_origin

    screenshot_dir = os.path.join(tempfile.gettempdir(), "clarityos")
    os.makedirs(screenshot_dir, exist_ok=True)
    fpath = os.path.join(screenshot_dir, f"vfind_{int(time.time())}.png")

    try:
        # ── 1. Detect active display (reuse Quartz logic from read_screen) ──
        display_num = 1
        logical_w, logical_h = None, None
        origin_x, origin_y = 0, 0
        try:
            if Quartz:
                max_d = 16
                err, ids, _ = Quartz.CGGetActiveDisplayList(max_d, None, None)
                if err == 0 and ids:
                    wl = Quartz.CGWindowListCopyWindowInfo(
                        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                        Quartz.kCGNullWindowID,
                    )
                    cx, cy = 0, 0
                    for w in (wl or []):
                        if w.get("kCGWindowLayer", 999) == 0 and w.get("kCGWindowOwnerName") != "Window Server":
                            b = w.get("kCGWindowBounds", {})
                            cx = b.get("X", 0) + b.get("Width", 0) / 2
                            cy = b.get("Y", 0) + b.get("Height", 0) / 2
                            break
                    r0 = Quartz.CGDisplayBounds(ids[0])
                    logical_w = int(r0.size.width)
                    logical_h = int(r0.size.height)
                    for i, did in enumerate(ids):
                        r = Quartz.CGDisplayBounds(did)
                        if (r.origin.x <= cx < r.origin.x + r.size.width and
                                r.origin.y <= cy < r.origin.y + r.size.height):
                            display_num = i + 1
                            logical_w = int(r.size.width)
                            logical_h = int(r.size.height)
                            origin_x = int(r.origin.x)
                            origin_y = int(r.origin.y)
                            break
        except Exception:
            pass

        _display_origin = {"x": origin_x, "y": origin_y}

        # ── 2. Capture screenshot ──
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", "-D", str(display_num), "-t", "png", fpath,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if not os.path.exists(fpath):
            return None

        # ── 3. Resize to logical resolution + encode JPEG ──
        import io as _io
        try:
            from PIL import Image
            img = Image.open(fpath).convert("RGB")
            os.remove(fpath)
            if logical_w and logical_h and img.size != (logical_w, logical_h):
                img = img.resize((logical_w, logical_h), Image.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            img_bytes = buf.getvalue()
        except ImportError:
            with open(fpath, "rb") as f:
                img_bytes = f.read()
            os.remove(fpath)

        # ── 4. Ask Flash to locate the element ──
        from google import genai
        from google.genai import types as _gtypes

        client = genai.Client()
        res_hint = ""
        if logical_w and logical_h:
            res_hint = f" Image is {logical_w}×{logical_h}. Coordinates are pixels from top-left."
        prompt = (
            f"Find the UI element best described as: '{description}'"
            f"{(' (context: ' + hint + ')') if hint else ''}.{res_hint}\n"
            f"Return ONLY the pixel coordinates of its CENTER as two integers: x,y\n"
            f"If you cannot find it, return exactly: NOTFOUND"
        )

        response = client.models.generate_content(
            model=_fast_model(),
            contents=[
                _gtypes.Content(
                    role="user",
                    parts=[
                        _gtypes.Part.from_text(text=prompt),
                        _gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    ],
                )
            ],
        )

        text = ""
        if response and response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text += part.text

        text = text.strip()
        if "NOTFOUND" in text.upper():
            print(f"[VisualLocate] ✗ '{description}' not found on screen")
            return None

        import re as _re_vl
        m = _re_vl.search(r"(\d{1,5})\s*[,;x]\s*(\d{1,5})", text)
        if m:
            vx, vy = int(m.group(1)), int(m.group(2))
            print(f"[VisualLocate] ✓ '{description}' found at ({vx}, {vy})")
            return (vx, vy)

        print(f"[VisualLocate] ✗ Could not parse coordinates from: {text[:80]}")
        return None

    except Exception as e:
        print(f"[VisualLocate] ⚠ Error: {e}")
        if os.path.exists(fpath):
            os.remove(fpath)
        return None

@registry.register(
    name="read_screen",
    description="Take a screenshot and analyze what's visible on screen using AI vision. Returns a description of the screen contents, including text, UI elements, buttons, and layout. Use when you need to understand what the user is looking at, read error messages, or identify clickable elements.",
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Optional question about the screen, e.g. 'What error message is shown?' or 'What buttons are visible?'"
            }
        },
        "required": []
    }
)
async def read_screen(question: str = "") -> str:
    global _display_origin, _screen_cache
    # Capture screenshot
    screenshot_dir = os.path.join(tempfile.gettempdir(), "clarityos")
    os.makedirs(screenshot_dir, exist_ok=True)
    filepath = os.path.join(screenshot_dir, f"screen_{int(time.time())}.png")
    
    try:
        # ── Detect active display using Quartz inline (no subprocess spawn) ──
        display_num = 1
        logical_w, logical_h = None, None
        origin_x, origin_y = 0, 0
        try:
            if Quartz:
                max_displays = 16
                err, display_ids, _ = Quartz.CGGetActiveDisplayList(max_displays, None, None)
                if err == 0 and display_ids:
                    # Find frontmost layer-0 window center
                    window_list = Quartz.CGWindowListCopyWindowInfo(
                        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                        Quartz.kCGNullWindowID,
                    )
                    active_cx, active_cy = 0, 0
                    for win in (window_list or []):
                        if win.get("kCGWindowLayer", 999) == 0 and win.get("kCGWindowOwnerName") != "Window Server":
                            b = win.get("kCGWindowBounds", {})
                            active_cx = b.get("X", 0) + b.get("Width", 0) / 2
                            active_cy = b.get("Y", 0) + b.get("Height", 0) / 2
                            break

                    # Which display contains that center?
                    r0 = Quartz.CGDisplayBounds(display_ids[0])
                    logical_w = int(r0.size.width)
                    logical_h = int(r0.size.height)
                    for i, did in enumerate(display_ids):
                        r = Quartz.CGDisplayBounds(did)
                        if (r.origin.x <= active_cx < r.origin.x + r.size.width and
                                r.origin.y <= active_cy < r.origin.y + r.size.height):
                            display_num = i + 1
                            logical_w = int(r.size.width)
                            logical_h = int(r.size.height)
                            origin_x = int(r.origin.x)
                            origin_y = int(r.origin.y)
                            break
        except Exception as e:
            print(f"[read_screen] Display detection error (using display 1): {e}")

        _display_origin = {"x": origin_x, "y": origin_y}

        # ── Capture the target display ──
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", "-D", str(display_num), "-t", "png", filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
        
        if not os.path.exists(filepath):
            return "ERROR: Failed to capture screenshot"

        # ── PIL pipeline: resize + hash + grid + encode (all in-process, no sips) ──
        # Using PIL avoids spawning sips (a slow subprocess on large Retina PNGs).
        # Sequence: load → resize to logical res → hash for cache → draw grid → encode.
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.open(filepath).convert("RGB")
            os.remove(filepath)

            # Resize to logical resolution (1 pixel = 1 click point)
            if logical_w and logical_h and img.size != (logical_w, logical_h):
                img = img.resize((logical_w, logical_h), Image.LANCZOS)

            # Hash the resized-but-ungridded image for cache comparison
            # Use a fast in-memory PNG (compress_level=0 = no compression, pure speed)
            import io
            raw_buf = io.BytesIO()
            img.save(raw_buf, format="PNG", optimize=False, compress_level=0)
            raw_bytes = raw_buf.getvalue()
            img_hash = hashlib.md5(raw_bytes).hexdigest()
            if img_hash == _screen_cache["hash"] and _screen_cache["result"]:
                return _screen_cache["result"]  # Screen unchanged, skip Vision API

            # Draw coordinate grid overlay (in-place on the PIL image)
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            w, h = img.size
            step = 100
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 11)
            except Exception:
                font = ImageFont.load_default()
            line_color = (255, 50, 50, 60)
            text_color = (255, 50, 50, 140)
            for x in range(step, w, step):
                draw.line([(x, 0), (x, h)], fill=line_color, width=1)
                draw.text((x + 2, 2), str(x), fill=text_color, font=font)
            for y in range(step, h, step):
                draw.line([(0, y), (w, y)], fill=line_color, width=1)
                draw.text((2, y + 2), str(y), fill=text_color, font=font)
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

            # Encode as JPEG (4-6x smaller than PNG, faster Vision API upload)
            out_buf = io.BytesIO()
            img.save(out_buf, format="JPEG", quality=85)
            img_data = base64.b64encode(out_buf.getvalue()).decode("utf-8")

        except ImportError:
            # PIL not available — fall back to raw bytes, no resize/grid
            with open(filepath, "rb") as f:
                raw_bytes = f.read()
            os.remove(filepath)
            img_hash = hashlib.md5(raw_bytes).hexdigest()
            if img_hash == _screen_cache["hash"] and _screen_cache["result"]:
                return _screen_cache["result"]
            img_data = base64.b64encode(raw_bytes).decode("utf-8")

        resolution_hint = ""
        if logical_w and logical_h:
            resolution_hint = (
                f" The image shows display {display_num} at logical resolution"
                f" ({logical_w} x {logical_h}). Pixel positions in this image"
                f" correspond 1:1 to click coordinates on that display."
            )
        
        # Use Gemini Vision to analyze
        # IMPORTANT: Use the POWERFUL model for screen reading — coordinate
        # precision is critical for click accuracy.  Flash models are too
        # imprecise for small UI targets like buttons, icons, and links.
        from google import genai
        from google.genai import types
        
        client = genai.Client()
        prompt = question or "Describe what's on this screen. Include any visible text, buttons, UI elements, error messages, and the overall layout. Be concise but thorough."
        prompt += (
            f"{resolution_hint} The image has a coordinate grid overlay with"
            f" labeled X values along the top and Y values along the left."
            f" Use these grid lines to output PRECISE (X, Y) pixel coordinates"
            f" for each clickable UI element, button, or link you describe."
            f" Read the grid labels carefully — the numbers on the lines tell you"
            f" the exact pixel value at that position. Interpolate between grid"
            f" lines for elements that fall between them."
            f" These coordinates will be used directly for mouse clicks."
        )
        
        response = client.models.generate_content(
            model=_powerful_model(),
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(
                            data=base64.b64decode(img_data),
                            mime_type="image/jpeg"
                        )
                    ]
                )
            ]
        )
        
        # Parse response parts manually to avoid warnings about thought_signature
        output_text = ""
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    output_text += part.text + "\n"
        
        result = output_text.strip()[:3000] if output_text else "Could not analyze screenshot"
        _screen_cache = {"hash": img_hash, "result": result}
        return result
        
    except Exception as e:
        return f"ERROR analyzing screen: {str(e)[:200]}"


# ── 17. click_element (GUI automation) ──
@registry.register(
    name="click_element",
    description="Click at specific screen coordinates (x, y). Use with read_screen to first identify where elements are, then click them. Coordinates are in pixels from top-left corner. Optionally double-click or right-click.",
    parameters={
        "type": "object",
        "properties": {
            "x": {
                "type": "integer",
                "description": "X coordinate (pixels from left edge)"
            },
            "y": {
                "type": "integer",
                "description": "Y coordinate (pixels from top edge)"
            },
            "click_type": {
                "type": "string",
                "description": "Type of click: 'single' (default), 'double', or 'right'",
                "enum": ["single", "double", "right"]
            }
        },
        "required": ["x", "y"]
    }
)
async def click_element(x: int, y: int, click_type: str = "single") -> str:
    try:
        # Apply display origin offset — read_screen captures a single display
        # and the Vision model returns coordinates relative to that image (0,0).
        # On multi-monitor setups the captured display may not start at (0,0)
        # in CGEvent's unified coordinate space, so we add the offset.
        x += _display_origin.get("x", 0)
        y += _display_origin.get("y", 0)

        # We run a small inline python script using Quartz for 100% reliable native clicks
        # This completely avoids the AppleScript "System Events" focus-stealing bugs.
        python_script = f"""
import time
import Quartz

x, y = {x}, {y}
click_type = "{click_type}"

def mouse_event(type, x, y, button=Quartz.kCGMouseButtonLeft):
    event = Quartz.CGEventCreateMouseEvent(None, type, (x, y), button)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

if click_type == "right":
    mouse_event(Quartz.kCGEventRightMouseDown, x, y, Quartz.kCGMouseButtonRight)
    time.sleep(0.05)
    mouse_event(Quartz.kCGEventRightMouseUp, x, y, Quartz.kCGMouseButtonRight)
elif click_type == "double":
    mouse_event(Quartz.kCGEventLeftMouseDown, x, y)
    mouse_event(Quartz.kCGEventLeftMouseUp, x, y)
    time.sleep(0.05)
    # Important: Double click requires specifying it's the second click
    event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 2)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    
    event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 2)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
else:
    # Single click
    mouse_event(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(0.05)
    mouse_event(Quartz.kCGEventLeftMouseUp, x, y)
"""
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", python_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            if "ModuleNotFoundError" in err and "Quartz" in err:
                return "ERROR: pyobjc-framework-Quartz is not installed. Run: pip install pyobjc-core pyobjc-framework-Quartz"
            return f"Click failed: {err[:200]}"
        
        return f"Clicked at ({x}, {y}) [{click_type}]"
    except Exception as e:
        return f"ERROR clicking: {str(e)[:200]}"


# ── 18. clipboard_ops ──
@registry.register(
    name="clipboard_ops",
    description=(
        "Interact with the macOS clipboard. Operations:\n"
        "- 'get': reads current clipboard text\n"
        "- 'set': writes text to clipboard\n"
        "- 'paste': triggers Cmd+V to paste at cursor position (works for both text AND images)\n"
        "- 'set_image': loads an image file onto the clipboard as image data (ready for Cmd+V paste into any app)\n"
        "- 'get_image_info': checks if the clipboard currently holds image data and returns its dimensions\n"
        "Use 'set_image' + 'paste' to paste images into Google Docs, Keynote, Slack, email, etc."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The clipboard operation",
                "enum": ["get", "set", "paste", "set_image", "get_image_info"]
            },
            "text": {
                "type": "string",
                "description": "Text to copy to clipboard (required for 'set' action)"
            },
            "image_path": {
                "type": "string",
                "description": "Path to image file (required for 'set_image' action). Use save_image to download first."
            }
        },
        "required": ["action"]
    }
)
async def clipboard_ops(action: str, text: str = "", image_path: str = "") -> str:
    try:
        if action == "get":
            proc = await asyncio.create_subprocess_exec(
                "pbpaste",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            content = stdout.decode("utf-8", errors="replace").strip()
            if content:
                return f"Clipboard contents:\n{content[:2000]}"
            return "Clipboard is empty (or contains non-text data like an image)"
            
        elif action == "set":
            if not text:
                return "ERROR: 'text' parameter required for 'set' action"
            proc = await asyncio.create_subprocess_exec(
                "pbcopy",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(
                proc.communicate(input=text.encode("utf-8")), timeout=3.0
            )
            return f"Copied {len(text)} characters to clipboard"
            
        elif action == "paste":
            # Trigger Cmd+V — works for text AND images on the pasteboard
            script = '''
            tell application "System Events"
                keystroke "v" using command down
            end tell
            '''
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=3.0)
            return "Pasted clipboard contents (text or image)"

        elif action == "set_image":
            if not image_path:
                return "ERROR: 'image_path' parameter required for 'set_image' action. Use save_image to download an image first."
            if not os.path.exists(image_path):
                return f"ERROR: Image file not found: {image_path}"
            return await _set_image_clipboard(image_path)

        elif action == "get_image_info":
            return await _get_clipboard_image_info()

        else:
            return f"ERROR: Unknown action '{action}'. Use 'get', 'set', 'paste', 'set_image', or 'get_image_info'."
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


async def _set_image_clipboard(image_path: str) -> str:
    """Load an image file onto the macOS pasteboard as image data."""
    # Use osascript with ObjC bridge to set NSPasteboard image data
    abs_path = os.path.abspath(image_path)
    script = f'''
    use framework "AppKit"
    use scripting additions

    set imagePath to POSIX file "{abs_path}"
    set theImage to current application's NSImage's alloc()'s initWithContentsOfFile:(POSIX path of imagePath)

    if theImage is missing value then
        return "ERROR: Could not load image from file"
    end if

    set pb to current application's NSPasteboard's generalPasteboard()
    pb's clearContents()
    pb's writeObjects:{{theImage}}

    set imgSize to theImage's |size|()
    set imgW to (imgSize's width) as integer
    set imgH to (imgSize's height) as integer
    return "OK:" & imgW & "x" & imgH
    '''
    result = await _osascript(script)
    if result and result.startswith("OK:"):
        dims = result[3:]
        return f"Image loaded onto clipboard ({dims} pixels). Use clipboard_ops(action='paste') or Cmd+V to paste it."
    return f"ERROR: Failed to load image onto clipboard: {result}"


async def _get_clipboard_image_info() -> str:
    """Check if the clipboard holds image data and return info."""
    script = '''
    use framework "AppKit"
    use scripting additions

    set pb to current application's NSPasteboard's generalPasteboard()
    set imgTypes to {current application's NSPasteboardTypePNG, current application's NSPasteboardTypeTIFF}
    set hasImage to false

    repeat with imgType in imgTypes
        if (pb's canReadItemWithDataConformingToTypes:{imgType}) then
            set hasImage to true
            exit repeat
        end if
    end repeat

    if not hasImage then
        return "NO_IMAGE"
    end if

    set imgData to pb's dataForType:(current application's NSPasteboardTypeTIFF)
    if imgData is missing value then
        set imgData to pb's dataForType:(current application's NSPasteboardTypePNG)
    end if
    if imgData is missing value then
        return "HAS_IMAGE:unknown_size"
    end if

    set theImage to current application's NSImage's alloc()'s initWithData:imgData
    if theImage is missing value then
        return "HAS_IMAGE:unknown_size"
    end if

    set imgSize to theImage's |size|()
    set imgW to (imgSize's width) as integer
    set imgH to (imgSize's height) as integer
    return "HAS_IMAGE:" & imgW & "x" & imgH
    '''
    result = await _osascript(script)
    if result and "HAS_IMAGE" in result:
        dims = result.split(":")[1] if ":" in result else "unknown"
        return f"Clipboard contains image data ({dims} pixels). Use clipboard_ops(action='paste') to paste it."
    return "Clipboard does not contain image data. Use save_image + clipboard_ops(action='set_image') to load one."


# ── 19. get_ui_tree (Accessibility UI Dump) ──
@registry.register(
    name="get_ui_tree",
    description="Dump the macOS Accessibility UI element tree for a window. This tells you EXACTLY what buttons, text fields, checkboxes, and menus exist on screen, along with their exact (x, y) coordinates. Highly recommended to use this before clicking to avoid guessing coordinates. Requires Accessibility permissions.",
    parameters={
        "type": "object", 
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Optional application name to target (e.g., 'CapCut'). If empty, uses the frontmost app."
            },
            "search_term": {
                "type": "string",
                "description": "Optional text to search for within the UI. If provided, only elements matching this term (case-insensitive) or their parents are returned. This prevents truncation when looking for specific buttons."
            }
        }
    }
)
async def get_ui_tree(app_name: str = "", search_term: str = "") -> str:
    # Build the AppleScript to get the target process
    target_block = 'set targetApp to first process whose frontmost is true'
    if app_name:
        target_block = f'set targetApp to process "{app_name}"'

    search_filter = ""
    if search_term:
        search_filter = f'set searchTerm to "{search_term.lower()}"\n'
    else:
        search_filter = 'set searchTerm to ""\n'

    script = f'''
    on run
        try
            tell application "System Events"
                {target_block}
                set targetWindow to front window of targetApp
                {search_filter}
                -- Helper to recursively dump UI elements
                return my dumpUI(targetWindow, "", searchTerm)
            end tell
        on error errMsg
            return "ERROR: " & errMsg
        end try
    end run
    
    on dumpUI(uiElem, indent, searchTerm)
        set theResult to ""
        try
            tell application "System Events"
                set eClass to class of uiElem as string
                set eRole to role of uiElem as string
                
                set eName to ""
                try
                    set eName to name of uiElem
                end try
                if eName is missing value then set eName to "unnamed"
                
                set ePos to {{0, 0}}
                try
                    set ePos to position of uiElem
                end try
                
                set eSize to {{0, 0}}
                try
                    set eSize to size of uiElem
                end try
                
                set eLine to "- [" & eRole & "] \\"" & eName & "\\" at " & (item 1 of ePos as string) & "," & (item 2 of ePos as string) & " (size: " & (item 1 of eSize as string) & "x" & (item 2 of eSize as string) & ")" & "\\n"
                
                set uiChildren to UI elements of uiElem
                set childResults to ""
                set hasMatchingChild to false
                
                repeat with childElem in uiChildren
                    set childOut to my dumpUI(childElem, indent & "  ", searchTerm)
                    if childOut is not "" then
                        set childResults to childResults & childOut
                        set hasMatchingChild to true
                    end if
                end repeat
                
                -- Filter logic: if search term is empty, always include.
                -- Otherwise, include if the element's name matches, or if any child matched.
                if searchTerm is "" then
                    set theResult to indent & eLine & childResults
                else
                    ignoring case
                        set isMatch to (eName contains searchTerm) or (eRole contains searchTerm)
                    end ignoring
                    if isMatch or hasMatchingChild then
                        set theResult to indent & eLine & childResults
                    end if
                end if
            end tell
        end try
        return theResult
    end dumpUI
    '''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        out = stdout.decode("utf-8").strip()
        err = stderr.decode("utf-8").strip()
        
        if proc.returncode != 0:
            if "not allowed" in err.lower() or "accessibility" in err.lower():
                return "ERROR: Accessibility permission required (System Settings → Privacy & Security → Accessibility)."
            return f"ERROR dumping UI tree: {err[:200]}"
            
        return out[:10000] if out else ("No elements found matching search." if search_term else "Window has no accessible UI elements.")
    except asyncio.TimeoutError:
        return "ERROR: Timed out getting UI tree"
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


# ── 20. press_key ──
@registry.register(
    name="press_key",
    description="Press a raw system key (like Tab, Enter, Escape, Arrows). Extremely useful for navigating forms, menus, and UIs without using the mouse or coordinates. Supported: 'return', 'tab', 'space', 'escape', 'up', 'down', 'left', 'right', 'delete'.",
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The key to press",
                "enum": ["return", "tab", "space", "escape", "up", "down", "left", "right", "delete"]
            },
            "times": {
                "type": "integer",
                "description": "Number of times to press the key (default: 1, max: 20)"
            }
        },
        "required": ["key"]
    }
)
async def press_key(key: str, times: int = 1) -> str:
    times = max(1, min(20, times))
    valid_keys = {
        "return": "return", "tab": "tab", "space": "space", "escape": "escape",
        "up": "126", "down": "125", "left": "123", "right": "124", "delete": "51"
    }
    
    if key not in valid_keys:
        return f"ERROR: Unsupported key '{key}'. Use tab, return, escape, etc."
        
    kcode = valid_keys[key]
    
    # Arrows and delete require key code, others use keystroke
    if key in ["up", "down", "left", "right", "delete"]:
        action = f"key code {kcode}"
    else:
        action = f"keystroke {kcode}"
        
    script = f'''
    tell application "System Events"
        repeat {times} times
            {action}
            delay 0.05
        end repeat
    end tell
    '''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode != 0:
            return f"ERROR pressing key: {stderr.decode('utf-8')[:200]}"
        return f"Pressed '{key}' {times} time(s)"
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


# ── 21. mouse_action ──
@registry.register(
    name="mouse_action",
    description="Perform advanced mouse actions: 'move' (hover over coordinates without clicking), 'scroll' (scroll up/down), or 'drag' (click and drag from x1,y1 to x2,y2).",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Action type: 'move', 'scroll', or 'drag'",
                "enum": ["move", "scroll", "drag"]
            },
            "x": {"type": "integer", "description": "X coordinate (for move/drag start)"},
            "y": {"type": "integer", "description": "Y coordinate (for move/drag start)"},
            "x2": {"type": "integer", "description": "Destination X (for drag only)"},
            "y2": {"type": "integer", "description": "Destination Y (for drag only)"},
            "lines": {"type": "integer", "description": "Lines to scroll (positive=up, negative=down)"}
        },
        "required": ["action"]
    }
)
async def mouse_action(action: str, x: int = 0, y: int = 0, x2: int = 0, y2: int = 0, lines: int = 5) -> str:
    try:
        if action == "move":
            py_script = f"""
import Quartz
moveEvent = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, ({x}, {y}), Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, moveEvent)
            """
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", py_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return f"Moved mouse to ({x}, {y})"
            
        elif action == "scroll":
            script = f'''
            tell application "System Events"
                scroll {"up" if lines > 0 else "down"} {abs(lines)}
            end tell
            '''
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return f"Scrolled {'up' if lines > 0 else 'down'} {abs(lines)} lines"
            
        elif action == "drag":
            py_script = f"""
import Quartz
import time

start_pos = ({x}, {y})
end_pos = ({x2}, {y2})

# Move to start
moveEvent = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, start_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, moveEvent)
time.sleep(0.1)

# Mouse down
downEvent = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, start_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, downEvent)
time.sleep(0.1)

# Mouse drag
dragEvent = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, end_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, dragEvent)
time.sleep(0.1)

# Mouse up
upEvent = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, end_pos, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, upEvent)
            """
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", py_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=3.0)
            return f"Dragged from ({x}, {y}) to ({x2}, {y2})"
            
        return f"Unknown action '{action}'"
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


# ── 22. window_manager ──
@registry.register(
    name="window_manager",
    description="Manage the active window's position and size. Actions: 'get' (returns current x, y, width, height), 'move' (moves to x, y preserving size), 'resize' (sets width and height preserving position), 'layout' (sets standard layouts like 'left_half', 'right_half', 'fullscreen').",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Action type: 'get', 'move', 'resize', or 'layout'",
                "enum": ["get", "move", "resize", "layout"]
            },
            "x": {"type": "integer", "description": "X coordinate for 'move'"},
            "y": {"type": "integer", "description": "Y coordinate for 'move'"},
            "width": {"type": "integer", "description": "Width for 'resize'"},
            "height": {"type": "integer", "description": "Height for 'resize'"},
            "layout_type": {
                "type": "string",
                "description": "Preset layout for 'layout' action",
                "enum": ["left_half", "right_half", "fullscreen", "center"]
            }
        },
        "required": ["action"]
    }
)
async def window_manager(action: str, x: int = 0, y: int = 0, width: int = 800, height: int = 600, layout_type: str = "") -> str:
    try:
        if action == "get":
            script = '''
            tell application "System Events"
                set frontApp to first process whose frontmost is true
                set fw to front window of frontApp
                set p to position of fw
                set s to size of fw
                return (item 1 of p as string) & "," & (item 2 of p as string) & "," & (item 1 of s as string) & "," & (item 2 of s as string)
            end tell
            '''
            proc = await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            res = stdout.decode("utf-8").strip()
            if not res: return "Failed to get window bounds"
            p = res.split(',')
            return f"Active window is at x={p[0]}, y={p[1]} with size {p[2]}x{p[3]}"
            
        elif action == "move":
            script = f'''
            tell application "System Events"
                set position of front window of (first process whose frontmost is true) to {{{x}, {y}}}
            end tell
            '''
            await asyncio.create_subprocess_exec("osascript", "-e", script)
            return f"Moved window to ({x}, {y})"
            
        elif action == "resize":
            script = f'''
            tell application "System Events"
                set size of front window of (first process whose frontmost is true) to {{{width}, {height}}}
            end tell
            '''
            await asyncio.create_subprocess_exec("osascript", "-e", script)
            return f"Resized window to {width}x{height}"
            
        elif action == "layout":
            bounds_script = 'tell application "Finder" to get bounds of window of desktop'
            proc = await asyncio.create_subprocess_exec("osascript", "-e", bounds_script, stdout=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            bounds = stdout.decode("utf-8").strip().replace(' ', '').split(',')  # x1,y1,x2,y2
            if len(bounds) != 4: return "Error getting screen bounds"
            
            w_total = int(bounds[2])
            h_total = int(bounds[3])
            
            if layout_type == "fullscreen":
                lx, ly, lw, lh = 0, 25, w_total, h_total - 25 # roughly account for menu bar
            elif layout_type == "left_half":
                lx, ly, lw, lh = 0, 25, w_total // 2, h_total - 25
            elif layout_type == "right_half":
                lx, ly, lw, lh = w_total // 2, 25, w_total // 2, h_total - 25
            elif layout_type == "center":
                lw, lh = int(w_total * 0.7), int(h_total * 0.8)
                lx, ly = (w_total - lw) // 2, (h_total - lh) // 2 + 10
            else:
                return f"Unknown layout {layout_type}"
                
            script = f'''
            tell application "System Events"
                set fw to front window of (first process whose frontmost is true)
                set position of fw to {{{lx}, {ly}}}
                set size of fw to {{{lw}, {lh}}}
            end tell
            '''
            await asyncio.create_subprocess_exec("osascript", "-e", script)
            return f"Applied layout '{layout_type}' (pos: {lx},{ly} size: {lw}x{lh})"
            
        return f"Unknown action: {action}"
    except Exception as e:
        return f"ERROR managing window: {str(e)[:100]}"


# ═══════════════════════════════════════════════════════════════
#  UI Tree Cache (shared by click_ui, type_in_field, etc.)
# ═══════════════════════════════════════════════════════════════

import re as _re

# Cache for parsed UI tree elements — invalidated on window change.
_ui_tree_cache = {
    "app": None,        # active app when cached
    "title": None,      # window title when cached
    "timestamp": 0.0,   # when the cache was populated
    "raw": "",          # raw output from get_ui_tree
    "elements": [],     # list of parsed element dicts
}

_UI_TREE_TTL = 3.0  # seconds before cache expires


def _parse_ui_elements(raw_tree: str) -> list[dict]:
    """Parse get_ui_tree output into structured element dicts.
    
    Each line looks like:
      - [AXButton] "Import" at 423,89 (size: 67x32)
    """
    elements = []
    for line in raw_tree.splitlines():
        m = _re.search(
            r'\[(\w+)\]\s+"([^"]*)"\s+at\s+(\d+),(\d+)\s+\(size:\s+(\d+)x(\d+)\)',
            line,
        )
        if m:
            role, name, x, y, w, h = m.groups()
            elements.append({
                "role": role,
                "name": name,
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
                "cx": int(x) + int(w) // 2,
                "cy": int(y) + int(h) // 2,
            })
    return elements


async def _get_cached_ui_tree(app_name: str = "", search_term: str = "") -> tuple[list[dict], str]:
    """Return parsed UI elements, using cache when valid.
    
    Returns:
        Tuple of (elements_list, raw_tree_string)
    """
    from agent.perception import get_active_app, get_window_title

    now = time.time()
    current_app = app_name or await get_active_app()
    current_title = await get_window_title()

    # ── Timeout circuit breaker: skip apps whose tree consistently times out ──
    app_lower = current_app.lower()
    if app_lower in _ui_tree_timeout_apps:
        cooldown_elapsed = now - _ui_tree_timeout_apps[app_lower]
        if cooldown_elapsed < _UI_TREE_TIMEOUT_COOLDOWN:
            print(f"[UI Tree] ⚡ Skipping {current_app} (timed out {cooldown_elapsed:.0f}s ago)")
            return [], "SKIPPED: accessibility tree previously timed out for this app"

    # Check cache validity — same app+window and not expired
    cache_valid = (
        _ui_tree_cache["app"] == current_app
        and _ui_tree_cache["title"] == current_title
        and (now - _ui_tree_cache["timestamp"]) < _UI_TREE_TTL
        and _ui_tree_cache["elements"]
        and not search_term  # always re-query with search_term for filtered results
    )

    if cache_valid:
        elements = _ui_tree_cache["elements"]
        raw = _ui_tree_cache["raw"]
    else:
        raw = await get_ui_tree(app_name=app_name, search_term=search_term)
        elements = _parse_ui_elements(raw)

        # Record timeout for circuit breaker
        if "timed out" in raw.lower():
            _ui_tree_timeout_apps[app_lower] = now
            print(f"[UI Tree] ⏱ Timeout recorded for {current_app} — will skip for {_UI_TREE_TIMEOUT_COOLDOWN:.0f}s")

        # Only update cache for unfiltered queries
        if not search_term:
            _ui_tree_cache.update({
                "app": current_app,
                "title": current_title,
                "timestamp": now,
                "raw": raw,
                "elements": elements,
            })

    # If search_term provided, filter elements client-side when using cache
    if search_term and cache_valid:
        term_lower = search_term.lower()
        elements = [e for e in elements if term_lower in e["name"].lower() or term_lower in e["role"].lower()]

    return elements, raw


def _best_match(elements: list[dict], description: str) -> Optional[dict]:
    """Find the best matching element for a textual description.
    
    Matching priority:
      1. Exact name match (case-insensitive)
      2. Name contains the description
      3. Description contains the element name
      4. Partial token overlap
    """
    desc_lower = description.lower().strip()
    desc_tokens = set(desc_lower.split())

    scored: list[tuple[int, dict]] = []
    for el in elements:
        name_lower = el["name"].lower()
        if not name_lower or name_lower == "unnamed":
            continue

        # Exact match
        if name_lower == desc_lower:
            return el  # perfect hit

        score = 0
        # Name contains description (e.g. "Import" matches "import")
        if desc_lower in name_lower:
            score += 80
        # Description contains name (e.g. "click the import button" contains "import")
        elif name_lower in desc_lower:
            score += 60
        # Token overlap
        else:
            name_tokens = set(name_lower.split())
            overlap = desc_tokens & name_tokens
            if overlap:
                score += len(overlap) * 20

        # Boost interactive roles
        if el["role"] in ("AXButton", "AXLink", "AXMenuItem", "AXPopUpButton",
                          "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton"):
            score += 10

        if score > 0:
            scored.append((score, el))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    return None


def _fallback_input_match(input_elements: list[dict], field_description: str) -> Optional[dict]:
    if not input_elements:
        return None

    desc_lower = (field_description or "").strip().lower()
    search_markers = ("search", "find", "lookup")
    compose_markers = ("message", "reply", "chat", "compose", "send", "text")

    def _pick(candidates: list[dict], *, reverse_y: bool = False) -> Optional[dict]:
        if not candidates:
            return None
        key_fn = (lambda el: (-int(el.get("y", 0)), int(el.get("x", 0)))) if reverse_y else (
            lambda el: (int(el.get("y", 0)), int(el.get("x", 0)))
        )
        return sorted(candidates, key=key_fn)[0]

    if any(marker in desc_lower for marker in search_markers):
        search_candidates = [
            el for el in input_elements
            if el.get("role") == "AXSearchField" or "search" in str(el.get("name", "")).lower()
        ]
        return _pick(search_candidates) or _pick(input_elements)

    if any(marker in desc_lower for marker in compose_markers):
        compose_candidates = [
            el for el in input_elements
            if el.get("role") == "AXTextArea"
            or any(marker in str(el.get("name", "")).lower() for marker in ("message", "reply", "chat"))
        ]
        return _pick(compose_candidates, reverse_y=True) or _pick(input_elements, reverse_y=True)

    return _pick(input_elements)


# ── 23. click_ui ──
@registry.register(
    name="click_ui",
    description=(
        "Click a UI element by its visible text or description. This tool uses the "
        "macOS Accessibility API to find the element's EXACT coordinates and click it — "
        "no screenshots or coordinate guessing needed. 5-10x faster and more accurate "
        "than read_screen + click_element. Supports single, double, and right clicks. "
        "Examples: click_ui(description='Import'), click_ui(description='Save As...')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "The visible text or label of the element to click (e.g. 'Import', 'Save', 'Open File', 'Login')"
            },
            "app_name": {
                "type": "string",
                "description": "Optional: target a specific app (default: frontmost app)"
            },
            "click_type": {
                "type": "string",
                "description": "Type of click: 'single' (default), 'double', or 'right'",
                "enum": ["single", "double", "right"]
            }
        },
        "required": ["description"]
    }
)
async def click_ui(description: str, app_name: str = "", click_type: str = "single") -> str:
    """Find a UI element by description and click it using the Accessibility API."""
    try:
        await _activate_target_app(app_name)

        # 1) Search accessibility tree
        elements, raw = await _get_cached_ui_tree(app_name=app_name, search_term=description)
        timed_out = "timed out" in (raw or "").lower() or "skipped" in (raw or "").lower()
        if not elements and not timed_out:
            elements, raw = await _get_cached_ui_tree(app_name=app_name, search_term="")

        # 2) Find best match from accessibility tree
        match = _best_match(elements, description) if elements else None

        if not match:
            # 3) Visual fallback — find the element on screen using Gemini Flash
            print(f"[click_ui] Accessibility miss for '{description}', trying visual fallback…")
            coords = await _fast_visual_locate(
                description,
                hint=f"in the {app_name} app" if app_name else "",
            )
            if coords:
                vx, vy = coords
                click_result = await click_element(x=vx, y=vy, click_type=click_type)
                return (
                    f"Visually located '{description}' at ({vx}, {vy}) and clicked [{click_type}]. "
                    f"{click_result}"
                )
            # Both accessibility and visual failed
            found_names = [e["name"] for e in elements[:10] if e.get("name") and e["name"] != "unnamed"] if elements else []
            return (
                f"No UI element matching '{description}' found (tried accessibility API + visual fallback). "
                f"{'Available elements: ' + ', '.join(found_names) + '. ' if found_names else ''}"
                f"Try a more specific description or use read_screen."
            )

        # 4) Click the center of the matched element
        cx, cy = match["cx"], match["cy"]
        click_result = await click_element(x=cx, y=cy, click_type=click_type)

        return (
            f"Clicked [{match['role']}] \"{match['name']}\" at ({cx}, {cy}) [{click_type}]. "
            f"{click_result}"
        )

    except Exception as e:
        return f"ERROR in click_ui: {str(e)[:200]}"


# ── 24. type_in_field ──
@registry.register(
    name="type_in_field",
    description=(
        "Click a text field by its label/description and type text into it. "
        "This combines finding the field via the Accessibility API, clicking it to focus, "
        "and typing the text — all in one step. Much faster and more reliable than "
        "manually finding coordinates. "
        "Example: type_in_field(field_description='Search', text='hello world')"
    ),
    parameters={
        "type": "object",
        "properties": {
            "field_description": {
                "type": "string",
                "description": "The label, placeholder, or description of the text field (e.g. 'Search', 'Email', 'Password', 'URL')"
            },
            "text": {
                "type": "string",
                "description": "The text to type into the field"
            },
            "app_name": {
                "type": "string",
                "description": "Optional: target a specific app (default: frontmost app)"
            },
            "clear_first": {
                "type": "boolean",
                "description": "If true, select-all and delete existing content before typing (default: false)"
            }
        },
        "required": ["field_description", "text"]
    }
)
async def type_in_field(field_description: str, text: str, app_name: str = "", clear_first: bool = False) -> str:
    """Find a text field, click to focus it, and type text."""
    try:
        await _activate_target_app(app_name)

        # 1) Find the field via accessibility API
        elements, raw = await _get_cached_ui_tree(app_name=app_name, search_term=field_description)
        timed_out = "timed out" in (raw or "").lower() or "skipped" in (raw or "").lower()
        if not elements and not timed_out:
            elements, raw = await _get_cached_ui_tree(app_name=app_name, search_term="")

        # Prefer text-input roles
        input_roles = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
        input_elements = [e for e in elements if e["role"] in input_roles]
        search_pool = input_elements if input_elements else elements

        match = None
        if search_pool:
            match = _best_match(search_pool, field_description)
            if not match and input_elements:
                match = _fallback_input_match(input_elements, field_description)

        # 2) If accessibility found a match, click it
        if match:
            cx, cy = match["cx"], match["cy"]
        else:
            # 3) Visual fallback — find the field using Gemini Flash
            print(f"[type_in_field] Accessibility miss for '{field_description}', trying visual fallback…")
            coords = await _fast_visual_locate(
                f"'{field_description}' text field or input area",
                hint=f"in the {app_name} app" if app_name else "",
            )
            if not coords:
                return (
                    f"No text field matching '{field_description}' found "
                    f"(tried accessibility API + visual fallback). "
                    f"Try click_ui on the target area first, then type_text."
                )
            cx, cy = coords

        # 4) Click the field to focus it
        await click_element(x=cx, y=cy)
        await asyncio.sleep(0.2)  # let focus settle

        # 5) Optionally clear existing content
        if clear_first:
            await _osascript(
                'tell application "System Events" to keystroke "a" using command down'
            )
            await asyncio.sleep(0.05)
            await _osascript(
                'tell application "System Events" to key code 51'  # delete
            )
            await asyncio.sleep(0.05)

        # 6) Type the text
        result = await type_text(text)

        source = f"[{match['role']}] \"{match['name']}\"" if match else f"(visual) '{field_description}'"
        return (
            f"Clicked {source} at ({cx}, {cy}), "
            f"then typed {len(text)} chars. {result}"
        )

    except Exception as e:
        return f"ERROR in type_in_field: {str(e)[:200]}"


# ═══════════════════════════════════════════════════════════════
#  Browser DOM Action Tools
# ═══════════════════════════════════════════════════════════════

async def _run_browser_js(js: str, app_name: str = "") -> str:
    """Execute JavaScript in the active browser tab.
    
    Auto-detects Chrome-family vs Safari and uses the correct AppleScript.
    Returns the JS evaluation result as a string.
    """
    if not app_name:
        from agent.perception import get_active_app
        app_name = await get_active_app()

    name_lower = app_name.lower()

    if "chrome" in name_lower or "chromium" in name_lower or "brave" in name_lower or "arc" in name_lower:
        script = f'tell application "{app_name}" to execute active tab of front window javascript "{js}"'
    elif "safari" in name_lower:
        script = f'tell application "Safari" to do JavaScript "{js}" in front document'
    else:
        return f"ERROR: '{app_name}' is not a supported browser for JS execution."

    return await _osascript(script)


# ── 25. browser_click ──
@registry.register(
    name="browser_click",
    description=(
        "LEGACY FALLBACK: Click an element inside a web page using a CSS selector or XPath. "
        "Prefer the browser extension flow (browser_snapshot -> browser_find -> browser_click_ref) "
        "unless the user explicitly provides a selector. "
        "Works with Chrome, Arc, Brave, Safari. "
        "Example: browser_click(selector='button.submit-btn') or browser_click(selector='#login-button')"
    ),
    parameters={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the element (e.g. '#login-btn', 'button.submit', 'a[href=\"/about\"]', 'input[name=\"email\"]')"
            },
            "selector_type": {
                "type": "string",
                "description": "'css' (default) or 'xpath'",
                "enum": ["css", "xpath"]
            }
        },
        "required": ["selector"]
    }
)
async def browser_click(selector: str, selector_type: str = "css") -> str:
    """Click a web page element via DOM."""
    try:
        # Escape single quotes in selector for AppleScript embedding
        safe_sel = selector.replace("'", "\\'").replace('"', '\\"')

        if selector_type == "xpath":
            js = (
                f"(function(){{ var el = document.evaluate('{safe_sel}', document, null, "
                f"XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; "
                f"if(el){{ el.click(); return 'clicked: ' + (el.textContent||'').substring(0,60); }} "
                f"else{{ return 'ERROR: no element found for xpath'; }} }})()"
            )
        else:
            js = (
                f"(function(){{ var el = document.querySelector('{safe_sel}'); "
                f"if(el){{ el.scrollIntoView({{block:'center'}}); el.click(); "
                f"return 'clicked: ' + (el.tagName||'') + ' ' + (el.textContent||'').substring(0,60); }} "
                f"else{{ return 'ERROR: no element found for selector'; }} }})()"
            )

        result = await _run_browser_js(js)
        if "ERROR" in result:
            return f"Could not find element matching '{selector}'. Check the selector or use get_ui_tree / read_screen."
        return f"Browser click: {result}"

    except Exception as e:
        return f"ERROR in browser_click: {str(e)[:200]}"


# ── 26. browser_fill ──
@registry.register(
    name="browser_fill",
    description=(
        "LEGACY FALLBACK: Fill a text input on a web page using a CSS selector. "
        "Prefer the browser extension flow (browser_snapshot -> browser_find -> browser_type_ref) "
        "unless the user explicitly provides a selector. "
        "Sets the value and dispatches input/change events so the page's JavaScript reacts correctly. "
        "Works with Chrome, Arc, Brave, Safari. "
        "Example: browser_fill(selector='input[name=\"email\"]', value='user@example.com')"
    ),
    parameters={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the input element"
            },
            "value": {
                "type": "string",
                "description": "Text to enter into the field"
            },
            "submit": {
                "type": "boolean",
                "description": "If true, press Enter after filling (submits the form). Default: false"
            }
        },
        "required": ["selector", "value"]
    }
)
async def browser_fill(selector: str, value: str, submit: bool = False) -> str:
    """Fill a web form field via DOM."""
    try:
        safe_sel = selector.replace("'", "\\'").replace('"', '\\"')
        safe_val = value.replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")

        submit_js = ""
        if submit:
            submit_js = (
                "var form = el.closest('form'); "
                "if(form){ form.submit(); } "
                "else { el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,bubbles:true})); }"
            )

        js = (
            f"(function(){{ var el = document.querySelector('{safe_sel}'); "
            f"if(!el) return 'ERROR: no element for selector'; "
            f"var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set; "
            f"nativeSetter.call(el, '{safe_val}'); "
            f"el.dispatchEvent(new Event('input', {{bubbles:true}})); "
            f"el.dispatchEvent(new Event('change', {{bubbles:true}})); "
            f"{submit_js}"
            f"return 'filled: ' + el.tagName + '[' + (el.name||el.id||'') + '] = ' + '{safe_val}'.substring(0,40); }})()"
        )

        result = await _run_browser_js(js)
        if "ERROR" in result:
            return f"Could not find input matching '{selector}'."
        return f"Browser fill: {result}"

    except Exception as e:
        return f"ERROR in browser_fill: {str(e)[:200]}"


# ═══════════════════════════════════════════════════════════════
#  Image Tools — download, capture, clipboard, paste
# ═══════════════════════════════════════════════════════════════

_IMAGE_DIR = os.path.join(tempfile.gettempdir(), "clarityos", "images")


def _ensure_image_dir():
    os.makedirs(_IMAGE_DIR, exist_ok=True)
    return _IMAGE_DIR


# ── 27. save_image ──
@registry.register(
    name="save_image",
    description=(
        "Download an image from a URL to a local temp file. Returns the local file path. "
        "Use this as the first step before copy_image_to_clipboard, clipboard_ops(set_image), "
        "or gdocs_insert_image. Supports JPEG, PNG, GIF, WebP, SVG."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL of the image to download"
            },
            "filename": {
                "type": "string",
                "description": "Optional filename (auto-detected from URL if omitted)"
            }
        },
        "required": ["url"]
    }
)
async def save_image(url: str, filename: str = "") -> str:
    import urllib.request
    import urllib.error
    from urllib.parse import urlparse, unquote

    _ensure_image_dir()

    # Determine filename
    if not filename:
        path = urlparse(url).path
        filename = os.path.basename(unquote(path)) or "image"
        # Ensure it has an extension
        if "." not in filename:
            filename += ".png"

    # Sanitize filename
    filename = "".join(c for c in filename if c.isalnum() or c in "._- ")[:100]
    filepath = os.path.join(_IMAGE_DIR, f"{int(time.time())}_{filename}")

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ClarityOS/1.0"
        })

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15))
        data = await loop.run_in_executor(None, response.read)

        with open(filepath, "wb") as f:
            f.write(data)

        size_kb = len(data) / 1024
        # Try to get image dimensions
        dims = ""
        try:
            from PIL import Image
            img = Image.open(filepath)
            dims = f", {img.width}×{img.height}px"
            img.close()
        except Exception:
            pass

        return json.dumps({
            "ok": True,
            "path": filepath,
            "size_kb": round(size_kb, 1),
            "filename": filename,
            "dimensions": dims.strip(", ") if dims else None,
            "message": f"Image saved to {filepath} ({size_kb:.1f} KB{dims}). "
                       f"Use clipboard_ops(action='set_image', image_path='{filepath}') to copy it, "
                       f"or gdocs_insert_image with a public URL."
        })
    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} downloading image: {str(e)[:200]}"
    except Exception as e:
        return f"ERROR: Failed to download image: {str(e)[:200]}"


# ── 28. copy_image_to_clipboard ──
@registry.register(
    name="copy_image_to_clipboard",
    description=(
        "Copy an image onto the macOS clipboard, ready for pasting into any app. "
        "Accepts a URL (downloads automatically) or a local file path. "
        "After calling this, use clipboard_ops(action='paste') to paste the image "
        "at the cursor position in Google Docs, Keynote, Slack, email, etc."
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Image URL (https://...) or local file path (/path/to/image.png)"
            }
        },
        "required": ["source"]
    }
)
async def copy_image_to_clipboard(source: str) -> str:
    image_path = source

    # If source is a URL, download first
    if source.startswith("http://") or source.startswith("https://"):
        result_json = await save_image(source)
        try:
            result = json.loads(result_json)
            if not result.get("ok"):
                return f"ERROR: Could not download image: {result_json}"
            image_path = result["path"]
        except (json.JSONDecodeError, KeyError):
            return f"ERROR: Could not download image: {result_json}"

    if not os.path.exists(image_path):
        return f"ERROR: Image file not found: {image_path}"

    # Load onto clipboard
    clipboard_result = await _set_image_clipboard(image_path)
    if "ERROR" in clipboard_result:
        return clipboard_result

    return f"Image copied to clipboard from '{os.path.basename(image_path)}'. {clipboard_result}"


# ── 29. capture_region_screenshot ──
@registry.register(
    name="capture_region_screenshot",
    description=(
        "Capture a screenshot of a specific rectangular region of the screen. "
        "Returns the file path of the captured image. Useful for capturing charts, "
        "tables, images, or specific UI sections. The image can then be copied to "
        "clipboard with clipboard_ops(action='set_image') and pasted elsewhere."
    ),
    parameters={
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Left edge X coordinate (pixels from left)"},
            "y": {"type": "integer", "description": "Top edge Y coordinate (pixels from top)"},
            "width": {"type": "integer", "description": "Width of the region in pixels"},
            "height": {"type": "integer", "description": "Height of the region in pixels"},
        },
        "required": ["x", "y", "width", "height"]
    }
)
async def capture_region_screenshot(x: int, y: int, width: int, height: int) -> str:
    _ensure_image_dir()
    filepath = os.path.join(_IMAGE_DIR, f"region_{int(time.time())}.png")

    # Validate dimensions
    if width <= 0 or height <= 0:
        return "ERROR: Width and height must be positive"
    if width > 10000 or height > 10000:
        return "ERROR: Region too large"

    try:
        # macOS screencapture -R x,y,w,h
        region = f"{x},{y},{width},{height}"
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", "-R", region, "-t", "png", filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)

        if not os.path.exists(filepath):
            return "ERROR: Failed to capture region screenshot"

        size_kb = os.path.getsize(filepath) / 1024
        return json.dumps({
            "ok": True,
            "path": filepath,
            "region": {"x": x, "y": y, "width": width, "height": height},
            "size_kb": round(size_kb, 1),
            "message": f"Captured {width}×{height} region at ({x},{y}). "
                       f"Use clipboard_ops(action='set_image', image_path='{filepath}') to copy it."
        })
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


# ═══════════════════════════════════════════════════════════════
#  send_imessage — visual iMessage via AppleScript
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="send_imessage",
    description=(
        "Send an iMessage to a contact via the macOS Messages app. "
        "Opens Messages visually so the user can see the message being sent. "
        "The recipient can be a phone number (with country code) or an email "
        "address registered with iMessage."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": (
                    "Phone number (e.g. '+447700900000') or email address "
                    "of the iMessage recipient."
                ),
            },
            "message": {
                "type": "string",
                "description": "The text message to send.",
            },
        },
        "required": ["recipient", "message"],
    },
    timeout=15.0,
)
async def send_imessage(recipient: str, message: str) -> str:
    """Send an iMessage visually via the macOS Messages app."""
    recipient = (recipient or "").strip()
    message = (message or "").strip()
    if not recipient:
        return json.dumps({"ok": False, "error": "Recipient is required."})
    if not message:
        return json.dumps({"ok": False, "error": "Message text is required."})
    if len(message) > 5000:
        message = message[:5000]

    # Bring Messages.app to the foreground so the user sees it
    await _activate_target_app("Messages")
    await asyncio.sleep(0.5)

    safe_recipient = _escape_applescript_string(recipient)
    safe_message = _escape_applescript_string(message)

    script = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{safe_recipient}" of targetService
        send "{safe_message}" to targetBuddy
    end tell
    '''

    try:
        result = await _osascript(script)
        if "error" in result.lower():
            # Fallback: try using the buddy approach
            fallback_script = f'''
            tell application "Messages"
                set targetService to 1st service whose service type = iMessage
                set theBuddy to buddy "{safe_recipient}" of targetService
                send "{safe_message}" to theBuddy
            end tell
            '''
            result = await _osascript(fallback_script)
            if "error" in result.lower():
                return json.dumps({
                    "ok": False,
                    "error": f"Failed to send iMessage: {result[:200]}",
                    "hint": "Check that the recipient has iMessage enabled and is a valid phone/email.",
                })

        return json.dumps({
            "ok": True,
            "message": f"Sent iMessage to {recipient}",
            "preview": message[:100],
        })
    except Exception as e:
        return json.dumps({
            "ok": False,
            "error": f"iMessage send failed: {str(e)[:200]}",
        })
