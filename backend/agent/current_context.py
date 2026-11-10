"""
ClarityOS — authoritative current-context snapshot
==================================================

One place that answers "what is the user actually looking at right now?", taken
once at the start of a request and then used to *bind* retrieval to it.

Why this exists
---------------
Context was being re-derived ad hoc at several points, so the agent could drift:
it would summarise a window it had just launched, read a stale browser tab from
a previous request, or treat a UI tree of VS Code's menu bar as the source file.
Each of those is the same bug — retrieved content that does not belong to the
context the user asked about.

This module does not retrieve content. It establishes identity: which app,
which window, which tab, which file, as of when, for which request. The
retrieval guard then refuses content whose identity does not match.

Everything here is best-effort and must degrade to "ambiguous" rather than
guess: reporting that the foreground could not be identified is always better
than binding the request to the wrong window.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

# A browser snapshot older than this is a previous navigation, not what is on
# screen now. The extension republishes on DOM change, so a fresh tab is
# normally only a second or two old.
SNAPSHOT_STALE_AFTER_S: float = 30.0

_BROWSER_APPS = {
    "google chrome": "chrome", "chrome": "chrome", "chromium": "chrome",
    "microsoft edge": "edge", "edge": "edge",
    "firefox": "firefox", "mozilla firefox": "firefox",
    "brave browser": "brave", "brave": "brave",
    "safari": "safari", "arc": "arc",
}

_EDITOR_APPS = {
    "visual studio code", "vscode", "code", "vscodium", "cursor",
    "visual studio", "sublime text", "notepad++", "pycharm",
    "intellij idea", "webstorm", "neovim", "vim", "emacs",
}


@dataclass
class CurrentContext:
    """Identity of what the user is looking at, as of one moment."""

    task_id: str = ""
    timestamp: float = field(default_factory=time.time)

    # Foreground window
    app: str = ""                 # "Google Chrome", "Visual Studio Code", …
    window_title: str = ""
    ambiguous: bool = False
    ambiguity_reason: str = ""

    # Browser, when the foreground app is one
    browser: str = ""             # canonical: chrome / edge / firefox / brave
    tab_id: str = ""
    url: str = ""
    page_title: str = ""
    snapshot_age_s: Optional[float] = None
    snapshot_fresh: bool = False
    # True when the tab came from the extension while some other app owned the
    # foreground — a screen recorder, a chat window. The page is still
    # readable; only keyboard/mouse automation would need focus.
    focus_elsewhere: bool = False

    # Editor, when the foreground app is one
    editor: str = ""
    file_name: str = ""
    file_path: str = ""
    workspace: str = ""

    @property
    def is_browser(self) -> bool:
        return bool(self.browser)

    @property
    def is_editor(self) -> bool:
        return bool(self.editor)

    def describe(self) -> str:
        """Identity only — never page or file contents."""
        if self.ambiguous:
            return f"foreground unidentified ({self.ambiguity_reason})"
        parts = [f"app={self.app or '?'}"]
        if self.is_browser:
            parts.append(f"browser={self.browser}")
            parts.append(f"tab={self.tab_id or '?'}")
            parts.append(f"url={(self.url or '?')[:60]}")
            parts.append(f"fresh={self.snapshot_fresh}")
        if self.is_editor:
            parts.append(f"editor={self.editor}")
            parts.append(f"file={self.file_name or '?'}")
        return " ".join(parts)


def _canonical_browser(app: str) -> str:
    return _BROWSER_APPS.get((app or "").strip().lower(), "")


def _is_editor(app: str) -> bool:
    return (app or "").strip().lower() in _EDITOR_APPS


def _title_key(text: str) -> str:
    """Lowercase alphanumerics only — tolerates the separators and suffixes
    Chrome adds to window titles without needing to model them."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())[:60]


def _browser_identity(ctx: CurrentContext) -> None:
    """Fill tab identity from the live Browser Bridge snapshot, if it is current."""
    try:
        from browser.store import browser_store
    except Exception:
        return
    snapshot = None
    try:
        snapshot = browser_store.get_snapshot()
    except Exception:
        snapshot = None
    if snapshot is None:
        ctx.ambiguity_reason = "the browser extension has not published a page yet"
        return

    age = time.time() - float(getattr(snapshot, "timestamp", 0) or 0)
    ctx.snapshot_age_s = age
    ctx.snapshot_fresh = age <= SNAPSHOT_STALE_AFTER_S
    ctx.tab_id = str(getattr(snapshot, "tab_id", "") or "")
    ctx.url = str(getattr(snapshot, "url", "") or "")
    ctx.page_title = str(getattr(snapshot, "title", "") or "")

    if not ctx.snapshot_fresh:
        # Keep the identity but mark it: a stale snapshot describes a tab the
        # user may have navigated away from, and reusing it was how a previous
        # request's page got summarised as if it were the current one.
        ctx.ambiguity_reason = (
            f"the last page snapshot is {age:.0f}s old, so it may not be the tab "
            f"currently in front"
        )
        return

    # Fresh is not the same as *the right tab*. The extension resolves the
    # active tab with chrome.tabs.query({lastFocusedWindow: true}), which picks
    # a tab from whichever Chrome window Chrome last considered focused — with
    # two Chrome windows open that is routinely not the one on screen. Observed:
    # a 1-second-old snapshot of web.whatsapp.com while the user was looking at
    # a Google results tab in a different window.
    #
    # The OS window title is the independent check: Chrome titles its window
    # "<tab title> - Google Chrome", so the live tab's title must appear there.
    if ctx.page_title and ctx.window_title:
        head = _title_key(ctx.page_title)
        if head and head not in _title_key(ctx.window_title):
            ctx.snapshot_fresh = False
            ctx.ambiguity_reason = (
                f"the extension is reporting the tab '{ctx.page_title[:60]}', but the "
                f"window in front of you is '{ctx.window_title[:60]}' — it is reading a "
                f"different Chrome window"
            )


def _editor_identity(ctx: CurrentContext) -> None:
    """Fill file identity from the window title via the existing safe parser."""
    try:
        from tools.editor_context import parse_editor_title, resolve_active_editor_file
    except Exception:
        return
    try:
        parsed = parse_editor_title(ctx.window_title)
    except Exception:
        parsed = None
    if not parsed:
        ctx.ambiguity_reason = "the editor's window title does not name a file"
        return
    ctx.file_name, ctx.workspace = parsed[0] or "", parsed[1] or ""

    # (path, reason); path is None when it cannot be resolved *safely* — the
    # sensitive-file and workspace rules live in editor_context and stay there.
    try:
        path, reason = resolve_active_editor_file(ctx.window_title)
    except Exception as exc:
        path, reason = None, f"could not resolve the file ({exc.__class__.__name__})"
    if path is not None:
        ctx.file_path = str(path)
    else:
        ctx.ambiguity_reason = reason


def capture(task_id: str = "") -> CurrentContext:
    """
    Identify the current foreground context. Never raises.

    Ambiguity is reported, not resolved by guessing: if the frontmost window
    cannot be established, downstream retrieval should say so rather than bind
    the request to whichever app happens to have tools available.
    """
    ctx = CurrentContext(task_id=task_id)

    try:
        from agent.window_scan import topmost_external_window
        found = topmost_external_window()
    except Exception as exc:
        found = None
        ctx.ambiguity_reason = f"window scan failed ({exc.__class__.__name__})"

    if not found:
        ctx.ambiguous = True
        ctx.ambiguity_reason = ctx.ambiguity_reason or (
            "no external window is in the foreground (ClarityOS itself may be in front)"
        )
        return ctx

    ctx.app, ctx.window_title = found[0] or "", found[1] or ""

    ctx.browser = _canonical_browser(ctx.app)
    if ctx.browser:
        _browser_identity(ctx)
    elif _is_editor(ctx.app):
        ctx.editor = ctx.app
        _editor_identity(ctx)
    else:
        # Something else owns the foreground — a screen recorder, a chat app,
        # ClarityOS's own window. That does not mean the browser is out of
        # reach: the extension publishes the active tab regardless of which
        # window has focus, and reading it needs no focus at all.
        #
        # Refusing here told a user recording their screen to "bring the
        # browser forward and ask again" while the tab was sitting there,
        # readable, the whole time.
        _browser_from_bridge(ctx)

    return ctx


def _browser_from_bridge(ctx: CurrentContext) -> None:
    """Adopt the extension's active tab when the foreground is not a browser.

    A stale snapshot is adopted too, with `snapshot_fresh` left False. That is
    deliberate: staleness means "do not trust this page *content*", not "there
    is no browser". The caller reacts to a stale snapshot by asking the
    extension to re-read — but that refresh is gated on a browser being
    identified at all, so bailing out here skipped it and the user was told to
    "bring the browser forward" instead.

    The extension only pushes on navigation and tab switches, and its service
    worker sleeps after ~30s idle. Someone reading an email while recording
    their screen is past that window almost immediately.
    """
    try:
        from browser.store import browser_store
        from tools.browser_target import bridge_browser
    except Exception:
        return
    try:
        snapshot = browser_store.get_snapshot()
    except Exception:
        snapshot = None
    if snapshot is None:
        return

    url = str(getattr(snapshot, "url", "") or "")
    if not url.startswith("http"):
        return

    age = time.time() - float(getattr(snapshot, "timestamp", 0) or 0)
    ctx.browser = bridge_browser() or "chrome"
    ctx.tab_id = str(getattr(snapshot, "tab_id", "") or "")
    ctx.url = url
    ctx.page_title = str(getattr(snapshot, "title", "") or "")
    ctx.snapshot_age_s = age
    ctx.snapshot_fresh = age <= SNAPSHOT_STALE_AFTER_S
    ctx.focus_elsewhere = True


# ── Binding ────────────────────────────────────────────────────────────────
# What the request is *about*, and whether a given context can answer it.

# Whole words only. These were plain substring tests, so "show the top three
# in a table" matched "tab" and the agent decided a fresh Amazon search was a
# question about the browser tab already open — then refused the task because
# the extension had not sent that tab's contents. Nothing about the request
# was ever about the open tab.
_BROWSER_SURFACE_RE = re.compile(
    r"(?i)\b(?:gmail|e-?mail|mail|page|web\s*page|tab|tabs|website|web\s*site|"
    r"browser|chrome|edge|firefox|url|link)\b"
)
_EDITOR_SURFACE_RE = re.compile(
    r"(?i)\b(?:vs\s*code|vscode|visual\s+studio\s+code|editor|code\s+on\s+screen|"
    r"this\s+file|current\s+file|open\s+file|source\s+code)\b"
)
_SCREEN_SURFACE_RE = re.compile(
    r"(?i)\b(?:screen|window)\b|\bwhat'?s\s+open\b"
)


def required_surface(user_request: str) -> str:
    """
    "browser", "editor", "screen" or "" — the surface the request names.

    Named explicitly by the user ("in Gmail", "this VS Code window") or implied
    ("the page currently open"). Returns "" when the request does not depend on
    what is on screen at all.
    """
    low = " ".join((user_request or "").lower().split())
    if not low:
        return ""
    if _EDITOR_SURFACE_RE.search(low):
        return "editor"
    if _BROWSER_SURFACE_RE.search(low):
        return "browser"
    if _SCREEN_SURFACE_RE.search(low):
        return "screen"
    return ""


def binding_problem(ctx: CurrentContext, user_request: str) -> str:
    """
    Why the current context cannot answer this request, or "" if it can.

    This is the check that stops the agent from opening a second window and
    answering from that instead: if the user asked about Gmail and the
    foreground is VS Code, the honest move is to say so.
    """
    want = required_surface(user_request)
    if not want:
        return ""

    if ctx.ambiguous:
        return (f"I couldn't tell which window is in front, so I can't be sure I'd be "
                f"looking at the right thing ({ctx.ambiguity_reason}).")

    if want == "browser" and not ctx.is_browser:
        return (f"You asked about something in the browser, but the window in front is "
                f"{ctx.app or 'not a browser'}. Bring the browser forward and ask again, "
                f"and I'll read the tab you're on.")

    if want == "editor" and not ctx.is_editor:
        return (f"You asked about code in an editor, but the window in front is "
                f"{ctx.app or 'not an editor'}.")

    if want == "browser" and ctx.is_browser and not ctx.snapshot_fresh:
        # Reached only after a refresh was already requested and still failed,
        # so do not tell the user to do something the agent just tried.
        where = (f"{ctx.browser.capitalize()} has the page open (behind {ctx.app})"
                 if ctx.focus_elsewhere else f"I can see {ctx.app} in front")
        return (f"{where}, but the ClarityOS extension did not send me this tab's "
                f"contents, even after I asked it to re-read. Open the extension "
                f"and press 'Capture snapshot', or reload the tab.")

    return ""
