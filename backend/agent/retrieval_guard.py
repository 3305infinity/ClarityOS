"""
ClarityOS — retrieval validation boundary
=========================================

Tool *invocation* succeeding is not the same as *content* being retrieved. The
agent has summarised a 58-character `ERROR:` string as though it were source
code, and claimed an editor was inspected when the editor read had refused.

This module classifies a raw tool result before it is allowed to reach
summarization. It is deliberately source-aware: a 40-character code snippet is
valid, while a 600-character error message is not, so length alone decides
nothing.

Shared by both execution paths (`core_v2` and `graph_agent`) so the rules
cannot drift. Browser payloads defer to the existing validator in
`tools.browser_tools` rather than reimplementing it.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Tool name -> logical source. Anything unlisted is treated as "other" and is
# only rejected on hard failure signals.
SOURCE_BY_TOOL: dict[str, str] = {
    "read_active_editor_file": "editor",
    "read_file": "editor",
    "get_ui_tree": "uia",
    "read_screen": "screenshot",
    "capture_region_screenshot": "screenshot",
    "browser_read_page": "browser",
    "browser_snapshot": "browser",
    "read_page_content": "browser",
    "get_page_summary": "browser",
    "extract_structured_data": "browser",
}

# Failure shapes tools actually emit. `error_envelope` stringifies as a Python
# dict repr, so JSON parsing is not enough.
_ERROR_PREFIXES = ("error:", "error ", "traceback")
_ENVELOPE_FAILURE = ("'ok': false", '"ok": false')

# Status text that carries no retrieved content.
_GENERIC_ONLY = re.compile(
    r"^\s*(?:ok|done|success|completed|no\s+content|nothing\s+to\s+(?:show|report)|"
    r"n/?a|null|none|\{\}|\[\])\s*$",
    re.IGNORECASE,
)


@dataclass
class Verdict:
    ok: bool
    source: str
    reason: str = ""
    partial: bool = False
    meta: dict = field(default_factory=dict)

    def user_message(self, app_hint: str = "") -> str:
        """Concise, honest explanation — never implies content was inspected."""
        if self.ok:
            return ""
        where = f" in {app_hint}" if app_hint else ""
        base = {
            "editor": f"I couldn't retrieve the active editor's source{where}.",
            "browser": "I couldn't retrieve the current page's content.",
            "screenshot": "I couldn't capture or read the screen.",
            "uia": f"I couldn't read usable content from the window{where}.",
        }.get(self.source, "I couldn't retrieve the content needed for that.")
        return f"{base} {self.reason}".strip()


# Accessibility roles that describe the window rather than its document, and
# the labels every editor/browser shows regardless of what is open.
_CHROME_ROLE_RE = re.compile(
    r"(?im)^\s*\[?(?:AX)?(?:Window|TitleBar|MenuBar|MenuItem|Menu|Button|Tab|"
    r"TabItem|ToolBar|StatusBar|ScrollBar|Separator|Image)\]?\b.*$"
)
_CHROME_LABEL_RE = re.compile(
    r"(?i)\b(?:file|edit|selection|view|go|run|terminal|help|window|"
    r"minimize|maximize|restore|close|explorer|search|source control|"
    r"extensions|problems|output|debug console|new tab|back|forward|reload)\b"
)


def _strip_chrome(text: str) -> str:
    """Drop window furniture so what remains is candidate document content."""
    body = _CHROME_ROLE_RE.sub("", text)
    body = _CHROME_LABEL_RE.sub("", body)
    return "\n".join(ln for ln in body.splitlines() if ln.strip()).strip()


# Evidence that text is source code rather than UI labels. Deliberately a count
# of distinct code signals, not a length test: three indented lines or three
# keywords look nothing like "File Edit Selection View Go Run Terminal Help",
# while a four-line function clears it easily.
_CODE_SIGNAL_RE = re.compile(
    r"(?m)^(?:[ ]{2,}|\t)\S"
    r"|\b(?:def|class|function|return|import|from|const|let|var|public|private|"
    r"async|await|elif|except|lambda|struct|impl|interface|namespace)\b"
    r"|[{};]\s*$|=>|::|==|!=|\+=|->|\(\)"
)

_MIN_CODE_SIGNALS = 3


def _has_code_evidence(text: str) -> bool:
    return len(_CODE_SIGNAL_RE.findall(text)) >= _MIN_CODE_SIGNALS


def _looks_like_error(text: str) -> bool:
    head = text.strip()[:400].lower()
    if any(head.startswith(p) for p in _ERROR_PREFIXES):
        return True
    return any(marker in head for marker in _ENVELOPE_FAILURE)


def classify(tool_name: str, raw: Any, *, expected_source: str = "") -> Verdict:
    """
    Decide whether a tool's result may be used as retrieved content.

    `expected_source` is what the request needs ("editor", "browser", …). When
    it differs from the tool's own source, the result is flagged: UI Automation
    window chrome must not stand in for the source code that was asked for.
    """
    source = SOURCE_BY_TOOL.get(tool_name, "other")
    text = "" if raw is None else str(raw)
    meta = {"tool": tool_name, "source": source, "chars": len(text)}

    if not text.strip():
        return Verdict(False, source, "The tool returned nothing.", meta=meta)

    if _looks_like_error(text):
        # Keep the tool's own wording: it is usually the actionable part.
        detail = text.strip().split("\n", 1)[0][:160]
        return Verdict(False, source, detail, meta=meta)

    if _GENERIC_ONLY.match(text):
        return Verdict(False, source, "The tool returned a status message, not content.", meta=meta)

    if source == "browser":
        payload = raw if isinstance(raw, dict) else None
        if payload is not None:
            if not payload.get("ok", True):
                return Verdict(False, "browser",
                               str(payload.get("message") or "Page retrieval failed."), meta=meta)
            try:
                from tools.browser_tools import _page_content_is_usable
                refusal = _page_content_is_usable(payload)
            except Exception:
                refusal = None
            if refusal is not None:
                return Verdict(False, "browser", str(refusal.get("message", "")), meta=meta)
            meta["url"] = payload.get("url") or ""
            meta["title"] = payload.get("title") or ""

    if source == "uia":
        # A UI tree is accessibility metadata, not the document. For a
        # source-code request it is only a usable fallback when the tree
        # actually carries code-bearing lines: menu labels, tab titles and
        # status-bar text describe the editor, they are not the code in it.
        body = _strip_chrome(text)
        if expected_source == "editor":
            if not _has_code_evidence(body):
                return Verdict(
                    False, "uia",
                    "The UI tree exposed only window chrome and editor labels, "
                    "not the file's code.",
                    meta=meta)
            # Real code is present, but it came from the accessibility tree
            # rather than the file, so it may be clipped to what is on screen.
            return Verdict(True, "uia",
                           "Read from the editor's on-screen text, so it covers "
                           "only the visible portion of the file.",
                           partial=True, meta=meta)
        if expected_source == "browser" and not body:
            return Verdict(False, "uia",
                           "Only window chrome was exposed, not the page content.",
                           meta=meta)

    if source == "editor":
        meta["has_path"] = bool(re.search(r"^Path:\s*\S", text, re.MULTILINE))
        if "<redacted>" in text and len(text) < 200:
            return Verdict(False, "editor",
                           "The file's contents were redacted as secrets.", meta=meta)

    if expected_source and source not in ("other", expected_source):
        return Verdict(True, source,
                       f"Answered from {source} content; the request asked about "
                       f"the {expected_source}.",
                       partial=True, meta=meta)

    return Verdict(True, source, meta=meta)


def same_request(meta: dict, task_id: str) -> bool:
    """Stale-context guard: content must belong to the current request."""
    recorded = (meta or {}).get("task_id")
    return recorded is None or recorded == task_id


# ───────────────────────────────────────────────────────────────────────────
#  Per-task retrieval ledger
# ───────────────────────────────────────────────────────────────────────────
# Progress messages are written by the planner *before* anything runs, so the
# user was told "Summarizing the code…" while the editor read had already been
# refused. The emitter needs to know what has actually been retrieved, and it
# sits in a different module from the guard — a contextvar keeps that shared
# without threading new arguments through MilestoneExecutor's signature.


@dataclass
class _Ledger:
    ok: int = 0
    failed: int = 0
    last_failure: str = ""
    notes: list = field(default_factory=list)
    last_progress: str = ""
    last_family: str = ""
    seen_urls: set = field(default_factory=set)
    rejected_urls: set = field(default_factory=set)


def tool_family(tool: str) -> str:
    """Which retrieval method a tool belongs to — used to narrate a switch."""
    name = (tool or "").lower()
    if name.startswith("browser_") or name in {"read_page_content", "get_page_summary",
                                               "fetch_web_content", "web_scrape"}:
        return "browser"
    if name in {"read_screen", "capture_region_screenshot"}:
        return "screenshot"
    if name == "get_ui_tree":
        return "ui tree"
    if name.startswith(("gmail_", "gdocs_", "gsheets_", "gdrive_", "gslides_",
                        "gcal_", "gworkspace")):
        return "workspace"
    if name in {"read_file", "read_active_editor_file"}:
        return "editor"
    return "other"


_ledger: contextvars.ContextVar[Optional[_Ledger]] = contextvars.ContextVar(
    "clarityos_retrieval_ledger", default=None
)


def reset_retrieval_state() -> None:
    """Begin a new task. A verdict from the previous one must not leak in."""
    _ledger.set(_Ledger())


def note_verdict(verdict: Verdict) -> None:
    led = _ledger.get()
    if led is None:
        led = _Ledger()
        _ledger.set(led)
    if verdict.ok:
        led.ok += 1
        if verdict.partial and verdict.reason:
            led.notes.append(verdict.reason)
    else:
        led.failed += 1
        led.last_failure = verdict.reason


def retrieval_attempted() -> bool:
    led = _ledger.get()
    return bool(led and (led.ok or led.failed))


def retrieval_succeeded() -> bool:
    led = _ledger.get()
    return bool(led and led.ok)


def last_failure_reason() -> str:
    led = _ledger.get()
    return led.last_failure if led else ""


def partial_notes() -> list:
    led = _ledger.get()
    return list(led.notes) if led else []


# ── URL grounding ──────────────────────────────────────────────────────────
# A screen read failed with 503 and the answer still contained a confident
# URL that the user opened and found wrong. The model had it from its own
# training data, not from the email. A URL may only be presented as "the link
# in this page/email" if it was actually seen in retrieved content.

_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\)}]+", re.IGNORECASE)


def _canonical(url: str) -> str:
    return str(url or "").strip().rstrip(".,);:'\"").rstrip("/").lower()


def note_content(text: str) -> None:
    """Record every URL that actually appeared in validated retrieved content."""
    led = _ledger.get()
    if led is None:
        led = _Ledger()
        _ledger.set(led)
    for match in _URL_RE.findall(str(text or ""))[:400]:
        led.seen_urls.add(_canonical(match))


def invalidate_url(url: str) -> None:
    """The user says this link was wrong. It must not be offered again."""
    led = _ledger.get()
    if led is None:
        led = _Ledger()
        _ledger.set(led)
    canon = _canonical(url)
    if canon:
        led.rejected_urls.add(canon)
        led.seen_urls.discard(canon)


def rejected_urls() -> set:
    led = _ledger.get()
    return set(led.rejected_urls) if led else set()


def ungrounded_urls(text: str) -> list:
    """
    URLs in *text* that were never seen in retrieved content, or that the user
    has already rejected. A non-empty result means the answer is asserting a
    destination nothing actually produced.
    """
    led = _ledger.get()
    if led is None:
        return []
    out: list = []
    for match in _URL_RE.findall(str(text or "")):
        canon = _canonical(match)
        if not canon:
            continue
        if canon in led.rejected_urls or canon not in led.seen_urls:
            if match not in out:
                out.append(match)
    return out


# Progress text that asserts work on content that must already be in hand.
# The second alternation covers claims about a specific artefact — "Send the
# extracted hackathon link" was announced with no link ever retrieved.
_DEPENDENT_RE = re.compile(
    r"(?i)\b(?:summari[sz]|present|report|explain|describe|analy[sz]|"
    r"compose|draft|write up|conclude|assess)\w*\b"
    r"|\b(?:the\s+)?(?:extracted|retrieved|collected|gathered|found)\s+\w+"
)
# Progress text that asserts a window was opened or focused.
_OPEN_CLAIM_RE = re.compile(r"(?i)\b(?:open|opening|launch|focus|focusing|switch)\w*\b")
_OPEN_TOOLS = frozenset({
    "open_app", "open_url", "web_search", "browser_switch_tab",
    "focus_window", "activate_app", "find_and_act",
})


def progress_text(description: str, tool: str = "") -> Optional[str]:
    """
    Rewrite a planned progress line to match what has actually happened.

    Returns the text to emit, or None when the claim is not yet true and no
    honest substitute exists — the caller should emit a failure status instead
    of a forward-looking promise.
    """
    text = (description or "").strip()
    if not text:
        return None

    if _DEPENDENT_RE.search(text) and not retrieval_succeeded():
        # Nothing has been retrieved yet, so there is nothing to summarize or
        # present. Say what is actually happening, or stay quiet.
        return None if retrieval_attempted() else "Working on it…"

    if _OPEN_CLAIM_RE.search(text) and tool and tool not in _OPEN_TOOLS:
        # The plan said "Opening and focusing VS Code" for a step that only
        # reads. Describe the tool that is really about to run.
        text = f"Running {tool.replace('_', ' ')}…"

    led = _ledger.get()
    if led is not None:
        family = tool_family(tool)
        # "Reading screen" nine times in a row told the user nothing and hid
        # the fact that the same call kept failing.
        if text == led.last_progress:
            return None
        # Switching retrieval method is the one repetition worth narrating.
        if (family and family != "other" and led.last_family
                and family != led.last_family and led.failed):
            text = f"{led.last_family.capitalize()} didn't work — trying {family}: {text}"
        led.last_progress = text
        if family:
            led.last_family = family

    return text
