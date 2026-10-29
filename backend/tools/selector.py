"""
Moonwalk — Tool Selector V2
============================
Intelligent tool selection using keyword matching and semantic categorization.
Reduces the 30+ tools down to a relevant subset for each request.
"""

import asyncio
import contextvars
import inspect
import json
import re
import time
from typing import Iterable, List, Set, Dict, Optional
from functools import partial
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from agent.browser_intent_utils import is_browser_chrome_action
from browser.interpreter_ai import (
    BrowserInterpretationError,
    summarize_scraped_page_with_flash,
)
from runtime_state import runtime_state_store
from tools.contracts import dumps as contract_dumps
from tools.contracts import error_envelope, success_envelope
from tools.route_policy import decide_web_route as decide_web_route_with_flash
from tools.search_policy import choose_search_result as choose_search_result_with_flash
from tools.registry import registry
from tools.registry import registry as tool_registry
from tools.browser_tools import _lookup_snapshot
from browser.store import browser_store

print = partial(print, flush=True)


def _norm_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_mixed_local_workflow(text: str, context_app: str = "") -> bool:
    normalized = _norm_text(f"{text} {context_app}")
    local_source_terms = (
        "downloads", "download", "desktop", "documents", "folder", "directory",
        "file", "latest", "newest", "most recent", "recent", "import",
        "video", "audio", "image", "photo", "clip", "recording",
    )
    app_terms = (
        "capcut", "final cut", "premiere", "resolve", "photoshop", "lightroom",
        "figma", "preview", "word", "excel", "pages", "numbers", "keynote",
        "cursor", "vscode", "visual studio code", "terminal", "finder",
    )
    non_browser_context = bool(context_app and context_app.lower() not in {
        "google chrome", "chrome", "safari", "arc", "brave", "firefox",
    })
    has_app = any(term in normalized for term in app_terms) or non_browser_context
    has_local_source = any(term in normalized for term in local_source_terms)
    return has_app and has_local_source


_TOOL_GATEWAY_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "moonwalk_tool_gateway_context",
    default={},
)

_WEB_PROGRESS_CALLBACK: contextvars.ContextVar = contextvars.ContextVar(
    "moonwalk_web_progress_callback",
    default=None,
)


def set_web_progress_callback(cb) -> None:
    """Set the async callback for web gateway progress updates."""
    _WEB_PROGRESS_CALLBACK.set(cb)


async def _emit_web_progress(text: str, variant: str = "browsing") -> None:
    """Emit a doing-state progress update to the Electron overlay."""
    cb = _WEB_PROGRESS_CALLBACK.get(None)
    if cb:
        try:
            await cb({
                "type": "doing",
                "text": text,
                "tool": "get_web_information",
                "variant": variant,
            })
        except Exception:
            pass

_ABSTRACT_WEB_INFO_TOOLS: frozenset[str] = frozenset({
    "web_search",
    "fetch_web_content",
    "web_scrape",
    "browser_read_page",
    "browser_read_text",
    "read_page_content",
    "extract_structured_data",
    "get_page_summary",
})

# Tools that should NEVER be culled from milestone scope — they are fundamental
# navigation/perception tools the LLM always needs access to.
# Tools that read an application that is ALREADY open, rather than starting one.
_INSPECTION_TOOLS: tuple[str, ...] = (
    "get_ui_tree", "read_screen", "capture_region_screenshot", "get_running_apps",
)

# Reading what is on a web page through the Browser Bridge. These see the DOM —
# including link destinations, which a screenshot cannot show at all. Measured:
# "find the hackathon link in the Gmail page currently open" selected 29 tools
# and not one of them was a browser tool, because "mail" routed the request to
# the Google Workspace category. The planner's only options were read_screen,
# get_ui_tree and capture_region_screenshot, so it looped on screenshots and
# never had a way to obtain an href.
_BROWSER_READ_TOOLS: tuple[str, ...] = (
    "browser_read_page", "browser_read_text", "browser_find",
    "browser_list_tabs", "browser_snapshot",
)

# Verbs that ask about existing state. Deliberately a verb class, not a phrase:
# the point is "the user wants to look, not launch".
_INSPECTION_VERBS: tuple[str, ...] = (
    "summarise", "summarize", "read", "what's open", "whats open", "what is open",
    "what's on", "whats on", "what is on", "explain", "describe", "check",
    "show me", "tell me about", "look at", "inspect", "analyse", "analyze",
    "visible", "on my screen", "currently",
)

_ALWAYS_AVAILABLE_TOOLS: frozenset[str] = frozenset({
    "send_response",
    "await_reply",
    "open_url",
    "open_app",
    "get_ui_tree",
})

_MILESTONE_HINT_EQUIVALENTS: dict[str, set[str]] = {
    # Web / research
    "get_web_information": {"get_web_information", "open_url", "browser_scroll", "web_scrape"},
    "open_url": {"open_url", "get_web_information", "browser_scroll"},
    "web_scrape": {"web_scrape", "get_web_information", "open_url"},
    # Google Workspace
    "gdocs_create": {"gdocs_create", "gdocs_append", "gdocs_read", "gworkspace_analyze"},
    "gdocs_append": {"gdocs_create", "gdocs_append", "gdocs_read"},
    "gdocs_read": {"gdocs_create", "gdocs_append", "gdocs_read", "gworkspace_analyze"},
    "gworkspace_analyze": {"gworkspace_analyze", "gdocs_read", "gsheets_read"},
    # File system
    "read_file": {"read_file", "list_directory", "run_shell"},
    "list_directory": {"list_directory", "read_file", "run_shell"},
    "write_file": {"write_file", "read_file", "replace_in_file", "list_directory", "run_shell"},
    "replace_in_file": {"replace_in_file", "read_file", "list_directory", "run_shell"},
    "run_shell": {"run_shell", "read_file", "list_directory", "write_file"},
    # App control (expanded: messaging tasks need UI tools after opening the app)
    "open_app": {"open_app", "get_ui_tree", "click_ui", "type_in_field", "type_text", "press_key", "read_screen"},
    # UI interaction (with cross-perception)
    "click_ui": {"click_ui", "type_in_field", "type_text", "press_key", "run_shortcut", "get_ui_tree", "open_app", "read_screen"},
    "type_in_field": {"type_in_field", "click_ui", "type_text", "press_key", "run_shortcut", "get_ui_tree", "open_app", "read_screen"},
    "type_text": {"type_text", "type_in_field", "click_ui", "press_key", "run_shortcut", "get_ui_tree"},
    "press_key": {"press_key", "run_shortcut", "click_ui", "type_in_field", "type_text", "open_app", "get_ui_tree"},
    "run_shortcut": {"run_shortcut", "press_key", "click_ui", "type_in_field", "type_text", "open_app", "get_ui_tree"},
    # Browser ref tools
    "browser_click_ref": {"browser_click_ref", "browser_type_ref", "browser_select_ref", "browser_snapshot", "browser_scroll"},
    "browser_type_ref": {"browser_type_ref", "browser_click_ref", "browser_select_ref", "browser_snapshot"},
    "browser_select_ref": {"browser_select_ref", "browser_click_ref", "browser_type_ref", "browser_snapshot"},
}

_BROWSER_APPS = frozenset({
    "google chrome",
    "chrome",
    "safari",
    "arc",
    "brave",
    "brave browser",
    "firefox",
    "microsoft edge",
    "edge",
})

_SEARCH_RESULT_HOSTS = frozenset({
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "search.yahoo.com",
})

_MESSAGING_APP_MARKERS = frozenset({
    "whatsapp",
    "messages",
    "imessage",
    "slack",
    "discord",
    "telegram",
    "signal",
    "messenger",
})

_DIRECT_MESSAGE_TOOLS = (
    "send_response",
    "await_reply",
    "open_app",
    "click_ui",
    "type_in_field",
    "type_text",
    "press_key",
    "run_shortcut",
    "get_ui_tree",
    "send_imessage",
)

_STICKY_ROUTE_TTL_S = 300.0
_ROUTE_STICKY_STATE: dict[str, dict] = {}


def set_tool_gateway_context(
    *,
    active_app: str = "",
    browser_url: str = "",
    background_mode: bool = False,
    browser_bridge_connected: bool = False,
    browser_has_snapshot: bool = False,
    browser_session_id: str = "",
) -> None:
    """Store lightweight runtime context for gateway tools."""
    runtime_state_store.update_os_state(
        active_app=(active_app or "").strip(),
        browser_url=(browser_url or "").strip(),
        provenance="browser_bridge" if browser_bridge_connected or browser_has_snapshot else ("applescript_fallback" if browser_url else ""),
        degraded=bool(browser_url) and not (browser_bridge_connected or browser_has_snapshot),
    )
    _TOOL_GATEWAY_CONTEXT.set(
        {
            "active_app": (active_app or "").strip(),
            "browser_url": (browser_url or "").strip(),
            "background_mode": bool(background_mode),
            "browser_bridge_connected": bool(browser_bridge_connected),
            "browser_has_snapshot": bool(browser_has_snapshot),
            "browser_session_id": (browser_session_id or "").strip(),
        }
    )


def _gateway_error(message: str, **extra) -> str:
    error_code = str(extra.pop("error_code", "") or "tool.unknown").strip()
    session_id = str(extra.get("session_id", "") or "").strip()
    payload = error_envelope(
        error_code,
        message,
        session_id=session_id,
        source="tool.get_web_information",
        details=extra,
        flatten_details=True,
    )
    return contract_dumps(payload)


def _safe_json(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def expand_milestone_hint_tools(hint_tools: Iterable[str]) -> set[str]:
    """Expand milestone hint tools into the runtime-equivalent tool family."""
    expanded: set[str] = set()
    for tool in hint_tools or []:
        normalized = str(tool).strip()
        if not normalized:
            continue
        expanded.add(normalized)
        expanded.update(_MILESTONE_HINT_EQUIVALENTS.get(normalized, set()))
    expanded.update({"send_response", "await_reply"})
    return expanded


def resolve_milestone_allowed_tools(
    hint_tools: Iterable[str],
    request_scope_tool_names: Optional[set[str]],
) -> tuple[Optional[set[str]], Optional[set[str]]]:
    """Two-tier tool access for milestones.

    Returns:
        (priority_tools, fallback_tools)
        - priority_tools: shown in the LLM prompt, preferred for this milestone
        - fallback_tools: all request-scope tools — accepted without penalty
        If request_scope is empty, returns (None, None) meaning "all tools allowed".
    """
    request_scope = set(request_scope_tool_names or set())
    if not request_scope:
        return None, None

    # Fallback = full request scope (always available)
    fallback = set(request_scope)

    expanded_hints = expand_milestone_hint_tools(hint_tools)
    if not expanded_hints:
        return request_scope, fallback

    # Priority = hint-matched tools intersected with scope + always-available
    priority = request_scope.intersection(expanded_hints)
    priority.update(_ALWAYS_AVAILABLE_TOOLS & request_scope)
    # If intersection was empty (bad hints), promote all to priority
    if not priority:
        priority = set(request_scope)

    return priority, fallback


def _domain(url: str) -> str:
    if not url:
        return ""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _sync_live_browser_context(context: Optional[dict]) -> dict:
    """Refresh lightweight gateway context from the live bridge/snapshot state."""
    live_context = dict(context or {})
    live_context.setdefault("browser_bridge_connected", False)
    live_context.setdefault("browser_has_snapshot", False)
    live_context.setdefault("browser_session_id", "")
    runtime_snapshot = runtime_state_store.snapshot()
    browser_state = runtime_snapshot.browser_state
    if browser_state.connected:
        live_context["browser_bridge_connected"] = True
    if browser_state.url:
        live_context["browser_has_snapshot"] = True
        live_context["browser_url"] = browser_state.url
    if browser_state.session_id:
        live_context["browser_session_id"] = browser_state.session_id
    try:
        from browser.bridge import browser_bridge
        from browser.store import browser_store
    except Exception:
        return live_context

    snapshot = browser_store.get_snapshot()
    if browser_bridge.is_connected():
        live_context["browser_bridge_connected"] = True
    if snapshot is not None:
        live_context["browser_has_snapshot"] = True
        # Prioritize the live snapshot URL over any cached context URL
        live_context["browser_url"] = str(getattr(snapshot, "url", "") or "").strip()
    session_id = (
        str(browser_bridge.connected_session_id() or "").strip()
        or str(getattr(snapshot, "session_id", "") or "").strip()
    )
    if session_id:
        live_context["browser_session_id"] = session_id
    return live_context


def _has_live_browser_bridge(context: Optional[dict]) -> bool:
    live_context = _sync_live_browser_context(context)
    return bool(
        live_context.get("browser_bridge_connected")
        or live_context.get("browser_has_snapshot")
        or live_context.get("browser_session_id")
    )


def _is_search_results_url(url: str) -> bool:
    if not url:
        return False
    host = _domain(url)
    if host not in _SEARCH_RESULT_HOSTS:
        return False
    lowered = url.lower()
    return "/search" in lowered or "q=" in lowered


# Known source-name → domain hints used to detect when a query explicitly asks
# for a different source than the cached selected_source_url.
_EXPLICIT_SOURCE_HINTS: dict[str, str] = {
    "wikipedia": "wikipedia.org",
    "wiki": "wikipedia.org",
    "imdb": "imdb.com",
    "reddit": "reddit.com",
    "youtube": "youtube.com",
    "twitter": "twitter.com",
    "github": "github.com",
    "stackoverflow": "stackoverflow.com",
    "metacritic": "metacritic.com",
    "rotten tomatoes": "rottentomatoes.com",
    "rottentomatoes": "rottentomatoes.com",
    "amazon": "amazon.com",
}


def _query_names_different_source(query: str, cached_url: str) -> bool:
    """Return True when the query explicitly names a source that doesn't match the cached URL.

    Used to bypass the request-state selected_source_url cache when the LLM is
    clearly asking for a different source (e.g. query contains 'Wikipedia' but
    the cache holds an IMDb URL).
    """
    q = (query or "").lower()
    cu = (cached_url or "").lower()
    for hint, domain in _EXPLICIT_SOURCE_HINTS.items():
        if hint in q and domain not in cu:
            return True
    return False


def _normalize_web_target_type(target_type: str) -> str:
    norm = _norm_text(target_type)
    alias_map = {
        "search": "search_results",
        "search result": "search_results",
        "search results": "search_results",
        "results": "search_results",
        "links": "search_results",
        "link list": "search_results",
        "page": "page_content",
        "content": "page_content",
        "article": "page_content",
        "article content": "page_content",
        "current page": "page_content",
        "current_page": "page_content",
        "summary": "page_summary",
        "page summary": "page_summary",
        "structured": "structured_data",
        "structured data": "structured_data",
        "listing": "structured_data",
        "listings": "structured_data",
    }
    return alias_map.get(norm, norm or "search_results")


def _prefer_browser_route(target_type: str, explicit_url: str, context: dict) -> bool:
    live_context = _sync_live_browser_context(context)
    if live_context.get("background_mode"):
        return False
    # When browser bridge is connected, always prefer browser
    return bool(_has_live_browser_bridge(live_context))


def _fallback_route_after_planner_error(target_type: str, explicit_url: str, context: dict) -> str:
    # When browser is connected, stay on browser_aci even in degraded mode
    if _prefer_browser_route(target_type, explicit_url, context):
        return "browser_aci"
    return "background_fetch"


def _urls_match_loose(left: str, right: str) -> bool:
    left_norm = _canonicalize_search_href(left).rstrip("/")
    right_norm = _canonicalize_search_href(right).rstrip("/")
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return left_norm.startswith(right_norm) or right_norm.startswith(left_norm)


def _match_selected_search_item(items: list[dict], selection: dict) -> Optional[dict]:
    selected_href = str(selection.get("selected_href", "") or "").strip()
    selected_ref_id = str(selection.get("selected_ref_id", "") or "").strip()
    selected_label = _norm_text(str(selection.get("selected_label", "") or ""))

    for item in items or []:
        if not isinstance(item, dict):
            continue
        if selected_ref_id and str(item.get("ref_id", "")).strip() == selected_ref_id:
            return item
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if selected_href and _urls_match_loose(str(item.get("href", "")).strip(), selected_href):
            return item
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if selected_label and _norm_text(str(item.get("label", "") or "")) == selected_label:
            return item
    return None


def _scrape_links_as_items(data: dict, max_items: int) -> list[dict]:
    items: list[dict] = []
    seen_urls: set[str] = set()
    for link in data.get("links", [])[: max(1, max_items * 3)]:
        if not isinstance(link, dict):
            continue
        label = " ".join(str(link.get("label", "")).split()).strip()
        href = _canonicalize_search_href(str(link.get("url", "")).strip())
        if not href:
            continue
        if _looks_like_search_engine_shell_url(href):
            continue
        if href in seen_urls:
            continue
        if label.lower().startswith("more at ") and href in seen_urls:
            continue
        domain = _domain(href)
        if not label:
            label = href
        items.append(
            {
                "rank": len(items) + 1,
                "label": label[:180],
                "href": href,
                "context": "",
                "href_domain": domain,
            }
        )
        seen_urls.add(href)
        if len(items) >= max(1, max_items):
            break
    return items


def _canonicalize_search_href(href: str) -> str:
    target = (href or "").strip()
    if not target:
        return ""
    parsed = urlparse(target)
    host = _domain(target)
    if host == "duckduckgo.com" and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query or "")
        uddg = qs.get("uddg", [])
        if uddg:
            return unquote(uddg[0]).strip()
    return target


def _looks_like_search_engine_shell_url(url: str) -> bool:
    if not url:
        return True
    host = _domain(url)
    lowered = url.lower()
    if host in {"duckduckgo.com", "html.duckduckgo.com"} and (
        "/html/" in lowered or lowered.endswith("duckduckgo.com/html")
    ):
        return True
    if host in _SEARCH_RESULT_HOSTS and ("/search" in lowered or "q=" in lowered):
        return True
    return False


def _route_sticky_key(target_type: str, context: dict, query: str = "", url: str = "") -> str:
    browser_domain = _domain(str(context.get("browser_url", "") or ""))
    if target_type == "search_results":
        return f"{target_type}|{browser_domain or 'browser'}"
    if url:
        return f"{target_type}|{_domain(url) or url[:60]}"
    return f"{target_type}|{browser_domain or 'generic'}"


def _get_sticky_route(key: str) -> Optional[dict]:
    if not key:
        return None
    entry = _ROUTE_STICKY_STATE.get(key)
    if not entry:
        return None
    if time.time() - float(entry.get("ts", 0.0) or 0.0) > _STICKY_ROUTE_TTL_S:
        _ROUTE_STICKY_STATE.pop(key, None)
        return None
    return entry


def _record_sticky_route(key: str, route: str, reason: str) -> None:
    if not key:
        return
    _ROUTE_STICKY_STATE[key] = {
        "route": route,
        "reason": reason[:220],
        "ts": time.time(),
    }


def _clear_sticky_route(key: str) -> None:
    if key:
        _ROUTE_STICKY_STATE.pop(key, None)


def _items_preview(items: list[dict], max_chars: int) -> str:
    rows: list[str] = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        context = str(item.get("context", "")).strip()
        href = str(item.get("href", "")).strip()
        row = " | ".join(part for part in (label, context, href) if part)
        if row:
            rows.append(row[:220])
    preview = "\n".join(rows)
    return preview[: max(80, max_chars)] if preview else ""


def _is_browser_search_infra_error(error_code: str) -> bool:
    return str(error_code or "").strip().lower() in {
        "no_snapshot",
        "stale_ref",
        "no_visible_elements",
        "flash_unavailable",
        "flash_timeout",
        "flash_error",
    }


def _has_deictic_source_reference(text: str) -> bool:
    normalized = f" {_norm_text(text)} "
    return any(
        marker in normalized
        for marker in (
            " it ",
            " this ",
            " that ",
            " these ",
            " those ",
            " clipboard ",
            " selected text ",
            " selection ",
            " use the link",
            " use this link",
            " use that link",
            " from the link",
            " from clipboard",
            " pasted link",
        )
    )


def _has_explicit_request_source(user_request: str, intent_target_value: str = "") -> bool:
    combined = _norm_text(f"{user_request} {intent_target_value}")
    if any(marker in combined for marker in ("http://", "https://", "www.", "youtube.com", "youtu.be")):
        return True
    return str(intent_target_value or "").strip().lower().startswith(("http://", "https://"))


def _looks_like_generic_media_open(
    user_request: str,
    *,
    intent_action: str = "",
    intent_target_type: str = "",
    intent_target_value: str = "",
) -> bool:
    action = _norm_text(intent_action)
    if action not in {"open", "play"}:
        return False
    if _has_deictic_source_reference(user_request):
        return False
    if _has_explicit_request_source(user_request, intent_target_value):
        return False

    target_type = _norm_text(intent_target_type)
    text = _norm_text(f"{user_request} {intent_target_value}")
    media_markers = (
        "video",
        "clip",
        "movie",
        "song",
        "music",
        "playlist",
        "podcast",
        "album",
        "youtube",
        "watch",
        "listen",
        "funny",
    )
    if target_type in {"content", "unknown", ""} and any(marker in text for marker in media_markers):
        return True
    return False


def _looks_like_direct_desktop_message(
    user_request: str,
    *,
    context_app: str = "",
    context_url: str = "",
    clipboard_content: str = "",
    selected_text: str = "",
    intent_action: str = "",
    intent_target_value: str = "",
) -> bool:
    if _norm_text(intent_action) != "communicate":
        return False
    combined_context = _norm_text(f"{user_request} {context_app} {intent_target_value}")
    if not any(marker in combined_context for marker in _MESSAGING_APP_MARKERS):
        return False
    if _has_deictic_source_reference(user_request):
        return False
    if _has_explicit_request_source(user_request, intent_target_value):
        return False
    for source_text in (context_url, clipboard_content, selected_text):
        if str(source_text or "").strip().lower().startswith(("http://", "https://", "www.")):
            return False
    return True


@registry.register(
    name="get_web_information",
    description=(
        "High-level web research gateway. Use this instead of raw web search, page read, "
        "or extraction tools. It automatically chooses the best backend route based on "
        "the current context: browser ACI when an interactive browser page is active, or "
        "direct background fetch/scrape when no browser context is available."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search topic or query. Required for search_results, optional for current-page reads.",
            },
            "target_type": {
                "type": "string",
                "description": "What kind of web information you want back.",
                "enum": ["search_results", "page_content", "page_summary", "structured_data"],
            },
            "url": {
                "type": "string",
                "description": "Optional URL to inspect directly instead of the current page.",
            },
            "item_hint": {
                "type": "string",
                "description": "Optional structured item hint such as results, products, listings, links, or table rows.",
            },
            "max_items": {
                "type": "integer",
                "description": "Maximum structured items to return (default 5).",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum readable content characters to return (default 5000).",
            },
        },
        "required": ["target_type"],
    },
)
async def get_web_information(
    query: str = "",
    target_type: str = "search_results",
    url: str = "",
    item_hint: str = "",
    max_items: int = 5,
    max_chars: int = 5000,
) -> str:
    """Abstract web research/search/read tool with backend routing."""
    target_type = _normalize_web_target_type(target_type)
    query = (query or "").strip()
    url = (url or "").strip()
    item_hint = (item_hint or "").strip() or "results"
    max_items = max(1, min(int(max_items or 5), 12))
    max_chars = max(300, min(int(max_chars or 5000), 12000))

    context = _sync_live_browser_context(_TOOL_GATEWAY_CONTEXT.get({}))
    sticky_key = _route_sticky_key(target_type, context, query=query, url=url)
    route_decision_reason = ""
    route_decision_model = ""
    route_decision_confidence = 0.0
    route_decision_degraded = False
    route_decision_error_code = ""
    sticky_route = _get_sticky_route(sticky_key)
    current_browser_url = str(context.get("browser_url", "") or "").strip()
    force_browser_search_follow = (
        bool(url)
        and not context.get("background_mode")
        and _is_search_results_url(current_browser_url)
        and target_type in {"page_content", "page_summary", "structured_data"}
    )
    if force_browser_search_follow:
        route = "browser_aci"
        route_decision_reason = "Following a visible source from the current live browser search-results page."
        route_decision_model = "search-follow-policy"
        route_decision_confidence = 0.99
        print(
            f"[WebGateway] route={route} degraded_mode=false "
            f"target_type={target_type} model=search-follow-policy"
        )
    elif sticky_route:
        route = str(sticky_route.get("route", "background_fetch")).strip() or "background_fetch"
        route_decision_reason = f"Sticky route fallback: {sticky_route.get('reason', '')}".strip()
        route_decision_model = "sticky-fallback"
        route_decision_confidence = 0.99
        print(
            f"[WebGateway] route={route} degraded_mode=false "
            f"target_type={target_type} model=sticky-fallback"
        )
    else:
        try:
            route_plan = decide_web_route_with_flash(
                target_type=target_type,
                query=query,
                url=url,
                item_hint=item_hint,
                context=context,
                runtime_state=runtime_state_store.snapshot().to_dict(),
            )
            if inspect.isawaitable(route_plan):
                route_plan = await route_plan
            route = str(route_plan.get("route", "")).strip() or "background_fetch"
            route_decision_reason = str(route_plan.get("reason", "")).strip()
            route_decision_model = str(
                route_plan.get("policy", "") or route_plan.get("_interpreter_model", "") or ""
            ).strip() or "deterministic-web-policy"
            try:
                route_decision_confidence = max(0.0, min(float(route_plan.get("confidence", 0.0) or 0.0), 1.0))
            except Exception:
                route_decision_confidence = 0.0
            print(
                f"[WebGateway] route={route} degraded_mode=false "
                f"target_type={target_type} "
                f"model={route_decision_model}"
            )
        except BrowserInterpretationError as exc:
            route = _fallback_route_after_planner_error(target_type, url, context)
            route_decision_reason = exc.reason
            route_decision_degraded = True
            route_decision_error_code = exc.error_code
            print(
                f"[WebGateway] route={route} degraded_mode=true "
                f"target_type={target_type} reason={exc.reason}"
            )

    def _apply_route_metadata(payload: dict, *, resolved_route: str) -> dict:
        payload.update(
            {
                "route": resolved_route,
                "target_type": target_type,
                "query": query,
                "route_decision_reason": route_decision_reason,
                "route_decision_model": route_decision_model,
                "route_decision_confidence": route_decision_confidence,
                "route_decision_degraded": route_decision_degraded,
            }
        )
        if route_decision_error_code:
            payload["route_decision_error_code"] = route_decision_error_code
        return payload

    def _gateway_success(payload: dict, *, resolved_route: str) -> str:
        applied = _apply_route_metadata(payload, resolved_route=resolved_route)
        envelope = success_envelope(
            applied,
            session_id=str(applied.get("session_id", "") or context.get("browser_session_id", "") or ""),
            provenance=resolved_route,
        )
        return contract_dumps(envelope)

    async def _background_search_payload(*, resolved_route: str = "background_fetch") -> str:
        if not query:
            return _gateway_error(
                "query is required for background search",
                target_type=target_type,
                route=resolved_route,
                error_code="missing_query",
            )
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        raw = await tool_registry.execute(
            "web_scrape",
            {"url": search_url, "max_chars": max_chars, "include_links": True},
        )
        data = _safe_json(raw)
        if data.get("ok") is False:
            return _gateway_error(
                data.get("message", "background search scrape failed"),
                target_type=target_type,
                route=resolved_route,
                error_code=data.get("error_code", "background_search_failed"),
                query=query,
            )
        items = _scrape_links_as_items(data, max_items)
        preview = _items_preview(items, max_chars)
        payload = {
            "ok": True,
            "url": data.get("url", search_url),
            "title": data.get("title", ""),
            "content": preview,
            "content_length": len(preview),
            "items": items,
            "item_count": len(items),
            "links": data.get("links", [])[:max_items],
        }
        _clear_sticky_route(sticky_key)
        return _gateway_success(payload, resolved_route=resolved_route)

    async def _background_page_payload(
        *,
        target_url: str,
        resolved_route: str = "background_fetch",
        follow_meta: Optional[dict] = None,
    ) -> str:
        if not target_url:
            return _gateway_error(
                "url is required for background page reads",
                target_type=target_type,
                route=resolved_route,
                error_code="missing_url",
            )
        raw = await tool_registry.execute(
            "web_scrape",
            {"url": target_url, "max_chars": max_chars, "include_links": True},
        )
        data = _safe_json(raw)
        if data.get("ok") is False:
            return _gateway_error(
                data.get("message", "background page scrape failed"),
                target_type=target_type,
                route=resolved_route,
                error_code=data.get("error_code", "background_page_failed"),
                url=target_url,
            )
        items = _scrape_links_as_items(data, max_items)
        payload = {
            "ok": True,
            "url": data.get("url", target_url),
            "title": data.get("title", ""),
            "content": data.get("content", "")[:max_chars],
            "content_length": int(data.get("content_length", 0) or 0),
            "links": data.get("links", [])[:max_items],
            "item_count": int(data.get("link_count", len(data.get("links", [])) or 0)),
        }
        if target_type == "page_summary":
            try:
                summary_data = await summarize_scraped_page_with_flash(
                    url=str(payload.get("url", "") or target_url),
                    title=str(payload.get("title", "") or ""),
                    content=str(data.get("content", "") or ""),
                    links=list(data.get("links", [])[:12]),
                )
            except BrowserInterpretationError as exc:
                return _gateway_error(
                    exc.reason,
                    target_type=target_type,
                    route=resolved_route,
                    error_code=exc.error_code,
                    url=target_url,
                )

            headings = summary_data.get("headings", []) if isinstance(summary_data, dict) else []
            heading_lines: list[str] = []
            if isinstance(headings, list):
                for heading in headings[:6]:
                    if not isinstance(heading, dict):
                        continue
                    text = str(heading.get("text", "")).strip()
                    if text:
                        heading_lines.append(text[:160])

            summary_text = str(summary_data.get("summary", "")).strip()
            content_parts = [part for part in [summary_text, "\n".join(heading_lines)] if part]
            payload["page_type"] = str(summary_data.get("page_type", "")).strip()
            payload["summary"] = summary_text
            payload["headings"] = headings if isinstance(headings, list) else []
            payload["key_targets"] = summary_data.get("key_targets", [])
            payload["confidence"] = summary_data.get("confidence", 0.0)
            payload["content"] = "\n".join(content_parts)[:max_chars]
            payload["content_length"] = len(payload["content"])
            payload["item_count"] = len(payload.get("headings", []) or payload.get("key_targets", []) or [])
        if target_type == "structured_data":
            payload["items"] = items
            preview = _items_preview(items, max_chars)
            if preview:
                payload["content"] = preview
                payload["content_length"] = len(preview)
                payload["item_count"] = len(items)
        if follow_meta:
            payload.update({k: v for k, v in follow_meta.items() if v not in (None, "", [])})
        _clear_sticky_route(sticky_key)
        return _gateway_success(payload, resolved_route=resolved_route)

    async def _choose_search_result_item(
        items: list[dict],
        *,
        page_url: str,
        page_title: str,
    ) -> tuple[Optional[dict], dict]:
        if not items:
            return None, {
                "search_follow_strategy": "none",
                "search_follow_degraded": True,
                "search_follow_reason": "No search result items were available to choose from.",
            }
        selection = choose_search_result_with_flash(
            items=items,
            query=query,
            target_type=target_type,
        )
        if inspect.isawaitable(selection):
            selection = await selection
        if isinstance(selection, tuple) and len(selection) == 2:
            chosen, follow_meta = selection
            return chosen, follow_meta
        if isinstance(selection, dict):
            chosen = _match_selected_search_item(items, selection)
            if chosen:
                return chosen, {
                    "search_follow_strategy": "deterministic-ranking",
                    "search_follow_degraded": False,
                    "search_follow_reason": str(selection.get("reason", "") or "").strip(),
                    "search_follow_confidence": float(selection.get("confidence", 0.0) or 0.0),
                    "search_follow_model": str(
                        selection.get("policy", "") or selection.get("_interpreter_model", "") or "deterministic-search-policy"
                    ).strip(),
                }
        return (items[0] if items else None), {
            "search_follow_strategy": "deterministic-first-result",
            "search_follow_degraded": True,
            "search_follow_reason": "No strong deterministic winner; using first result.",
            "search_follow_confidence": 0.35,
        }

    async def _browser_open_if_needed(target_url: str) -> str:
        if not target_url:
            return ""
        current_url = str(context.get("browser_url", "") or "").strip()
        if current_url.rstrip("/") == target_url.rstrip("/"):
            return str(context.get("browser_session_id", ""))

        domain_label = _domain(target_url) or target_url[:40]
        await _emit_web_progress(f"Opening {domain_label}…", "browsing")

        if _is_search_results_url(current_url):
            try:
                raw = await tool_registry.execute(
                    "extract_structured_data",
                    {"item_type": "search_result", "max_items": max(max_items, 8)},
                )
                data = _safe_json(raw)
                items = data.get("items", []) if isinstance(data.get("items"), list) else []
                match = None
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    href = str(item.get("href", "")).strip()
                    if _urls_match_loose(href, target_url):
                        match = item
                        break
                if match:
                    ref_id = str(match.get("ref_id", "")).strip()
                    label = str(match.get("label", "")).strip()
                    if ref_id:
                        await tool_registry.execute("browser_click_ref", {"ref_id": ref_id})
                    elif label:
                        await tool_registry.execute("browser_click_match", {"query": label})
                    else:
                        match = None
                    if match:
                        await asyncio.sleep(1.5)
                        try:
                            await tool_registry.execute("browser_refresh_refs", {})
                        except Exception:
                            pass
                        context["browser_url"] = target_url
                        return ""
            except Exception:
                pass

        await tool_registry.execute("open_url", {"url": target_url})
        
        # Wait up to 5 seconds for a snapshot whose URL actually matches the
        # TARGET path — not just the domain.  The old domain-only check would
        # match a pre-existing tab on the same domain (e.g. /details/dates
        # when we wanted /rules) and return immediately, skipping the wait.
        target_domain = _domain(target_url)
        target_path = urlparse(target_url).path.rstrip("/") or ""
        started = time.time()
        target_session_id = ""
        while time.time() - started < 5.0:
            await asyncio.sleep(0.25)
            # Check for a match in ANY known session snapshot
            match_sid = ""
            for sid, snap in getattr(browser_store, "_snapshots", {}).items():
                snap_url = str(getattr(snap, "url", "") or "")
                if not snap_url:
                    continue
                snap_domain = _domain(snap_url)
                snap_path = urlparse(snap_url).path.rstrip("/") or ""
                # Require both domain AND path to match the target
                if snap_domain == target_domain and (
                    snap_path == target_path
                    or target_path.startswith(snap_path)
                    or snap_path.startswith(target_path)
                ):
                    match_sid = sid
                    break
            if match_sid:
                target_session_id = match_sid
                break
            
            s_cand = _lookup_snapshot("")
            if s_cand:
                cand_url = str(s_cand.url or "")
                cand_domain = _domain(cand_url)
                cand_path = urlparse(cand_url).path.rstrip("/") or ""
                if cand_domain == target_domain and (
                    cand_path == target_path
                    or target_path.startswith(cand_path)
                    or cand_path.startswith(target_path)
                ):
                    target_session_id = str(s_cand.session_id)
                    break
                
        # Issue 4: JS-SPA navigation fallback — if we're on the right domain but
        # the target path is still not matched (e.g. devpost /rules rendered via a
        # JS tab-swap without a URL change), try clicking a nav element whose text
        # matches the last path segment (e.g. "rules", "prizes", "judges").
        if not target_session_id:
            s_cand = _lookup_snapshot("")
            if s_cand:
                cand_url = str(getattr(s_cand, "url", "") or "")
                if _domain(cand_url) == target_domain and target_path:
                    path_seg = target_path.split("/")[-1].strip().lower()
                    if path_seg:
                        try:
                            await tool_registry.execute(
                                "browser_click_match", {"query": path_seg}
                            )
                            await asyncio.sleep(1.5)
                            # Re-check snapshots after click
                            for sid, snap in getattr(browser_store, "_snapshots", {}).items():
                                snap_url2 = str(getattr(snap, "url", "") or "")
                                if snap_url2 and _domain(snap_url2) == target_domain and (
                                    target_path in urlparse(snap_url2).path
                                    or path_seg in snap_url2.lower()
                                ):
                                    target_session_id = sid
                                    break
                            if not target_session_id:
                                s2 = _lookup_snapshot("")
                                if s2 and (
                                    target_path in str(s2.url or "")
                                    or path_seg in str(s2.url or "").lower()
                                ):
                                    target_session_id = str(s2.session_id)
                        except Exception:
                            pass

        try:
            await tool_registry.execute("browser_refresh_refs", {"session_id": target_session_id})
        except Exception:
            pass
        return target_session_id

    async def _browser_search_results_payload() -> str:
        context.update(_sync_live_browser_context(context))
        target_session_id = ""
        if query:
            await _emit_web_progress(f"Searching: {query[:40]}…", "searching")
            await tool_registry.execute("web_search", {"query": query})
            
            # Wait up to 5 seconds for a search results snapshot
            started = time.time()
            while time.time() - started < 5.0:
                await asyncio.sleep(0.25)
                s_cand = _lookup_snapshot("")
                if s_cand and _is_search_results_url(str(s_cand.url or "")):
                    target_session_id = str(s_cand.session_id)
                    break
        elif not _is_search_results_url(str(context.get("browser_url", ""))):
            return _gateway_error(
                "query is required when no browser search-results page is active",
                target_type="search_results",
                route="browser_aci",
                error_code="missing_query_or_wrong_page",
            )
        else:
            target_session_id = str(context.get("browser_session_id", ""))

        last_error: dict = {}
        search_attempts = 0
        max_search_attempts = 3
        data: dict = {}

        for attempt in range(max_search_attempts):
            search_attempts = attempt + 1
            try:
                await tool_registry.execute("browser_refresh_refs", {"session_id": target_session_id})
            except Exception:
                pass

            raw = await tool_registry.execute(
                "extract_structured_data",
                {"item_type": "search_result", "query": query, "max_items": max_items, "session_id": target_session_id},
            )
            data = _safe_json(raw)
            if data.get("ok") is False:
                last_error = data
                if str(data.get("error_code", "")).strip().lower() in {"flash_timeout", "flash_error", "flash_unavailable"}:
                    break
            elif data.get("item_count", 0) or data.get("items"):
                data["ok"] = True
                data["item_count"] = int(data.get("item_count", len(data.get("items", [])) or 0))
                if isinstance(data.get("items"), list):
                    preview = _items_preview(data.get("items", []), max_chars)
                    if preview:
                        data["content"] = preview
                        data["content_length"] = len(preview)
                if data.get("url"):
                    context["browser_url"] = str(data.get("url") or "").strip()
                data["search_strategy"] = "browser_live"
                data["search_attempts"] = search_attempts
                _clear_sticky_route(sticky_key)
                return _gateway_success(data, resolved_route=route)
            else:
                last_error = {
                    "message": "No structured search results matched on the current browser page.",
                    "error_code": "empty_items",
                    "url": data.get("url", ""),
                    "title": data.get("title", ""),
                }

            if attempt >= max_search_attempts - 1:
                break

            scroll_raw = await tool_registry.execute("browser_scroll", {"direction": "down", "amount": "page", "session_id": target_session_id})
            scroll_data = _safe_json(scroll_raw)
            await asyncio.sleep(0.8)
            if bool(scroll_data.get("at_bottom")):
                break

        context.update(_sync_live_browser_context(context))
        error_code = str(last_error.get("error_code", "")).strip() or "browser_search_no_results"

        return _gateway_error(
            last_error.get("message", "Live browser search could not extract visible results."),
            target_type=target_type,
            route=route,
            error_code=error_code if error_code != "empty_items" else "browser_search_no_results",
            query=query,
            search_strategy="browser_live",
            search_attempts=search_attempts,
            url=last_error.get("url", ""),
            title=last_error.get("title", ""),
        )

    async def _search_follow_then_read(*, resolved_route: str) -> str:
        request_state = runtime_state_store.snapshot().request_state
        cached_source_url = str(getattr(request_state, "selected_source_url", "") or "").strip()
        cached_source_label = str(getattr(request_state, "selected_source_label", "") or "").strip()
        # Bypass cache when the query explicitly requests a different source domain
        # (e.g. query says 'wikipedia' but cache holds imdb.com) to avoid stalls.
        if cached_source_url and _query_names_different_source(query, cached_source_url):
            print(
                f"[WebGateway] cache bypass — query names a different source than "
                f"'{cached_source_url}'; doing fresh search"
            )
            cached_source_url = ""
            cached_source_label = ""
        if cached_source_url:
            follow_meta = {
                "selected_source_url": cached_source_url,
                "selected_source_label": cached_source_label,
                "search_follow_strategy": "request-state-reuse",
                "search_follow_degraded": False,
                "search_follow_reason": "Reused the previously selected source for this request.",
                "search_follow_confidence": 0.95,
                "search_follow_model": "request-state-cache",
            }
            if resolved_route == "browser_aci":
                await _browser_open_if_needed(cached_source_url)
                if target_type == "page_summary":
                    raw = await tool_registry.execute("get_page_summary", {})
                    data = _safe_json(raw)
                elif target_type == "structured_data":
                    raw = await tool_registry.execute(
                        "extract_structured_data",
                        {"item_type": item_hint, "query": query, "max_items": max_items},
                    )
                    data = _safe_json(raw)
                else:
                    raw = await tool_registry.execute(
                        "read_page_content",
                        {"max_chars": max_chars, "scroll_pages": 2},
                    )
                    data = _safe_json(raw)
                if data.get("ok") is not False and data:
                    data["ok"] = True
                    data.update({k: v for k, v in follow_meta.items() if v not in (None, "", [])})
                    _clear_sticky_route(sticky_key)
                    return _gateway_success(data, resolved_route=resolved_route)
            else:
                return await _background_page_payload(
                    target_url=cached_source_url,
                    resolved_route=resolved_route,
                    follow_meta=follow_meta,
                )

        if resolved_route == "browser_aci":
            search_payload_raw = await _browser_search_results_payload()
        else:
            search_payload_raw = await _background_search_payload(resolved_route=resolved_route)
        search_data = _safe_json(search_payload_raw)
        if search_data.get("ok") is False:
            return search_payload_raw

        items = search_data.get("items", []) if isinstance(search_data.get("items"), list) else []
        chosen_item, follow_meta = await _choose_search_result_item(
            items,
            page_url=str(search_data.get("url", "") or ""),
            page_title=str(search_data.get("title", "") or ""),
        )
        if not chosen_item:
            return _gateway_error(
                "Search succeeded but no result could be selected to follow.",
                target_type=target_type,
                route=resolved_route,
                error_code="search_follow_no_selection",
                query=query,
            )

        chosen_url = str(chosen_item.get("href", "") or "").strip()
        chosen_label = str(chosen_item.get("label", "") or "").strip()
        follow_meta = dict(follow_meta or {})
        follow_meta["selected_source_url"] = chosen_url
        follow_meta["selected_source_label"] = chosen_label
        runtime_state_store.update_request_state(
            selected_source_url=chosen_url,
            selected_source_label=chosen_label,
            search_results=items,
        )
        if not chosen_url:
            return _gateway_error(
                "Selected search result has no URL to follow.",
                target_type=target_type,
                route=resolved_route,
                error_code="search_follow_missing_url",
                query=query,
                selected_source_label=chosen_label,
            )

        if resolved_route == "browser_aci":
            await _browser_open_if_needed(chosen_url)

            chosen_domain = _domain(chosen_url) or chosen_label[:30] or "page"

            if target_type == "page_summary":
                await _emit_web_progress(f"Analyzing {chosen_domain}…", "browsing")
                raw = await tool_registry.execute("get_page_summary", {})
                data = _safe_json(raw)
                if data.get("ok") is False:
                    return _gateway_error(
                        data.get("message", "page summary failed"),
                        target_type=target_type,
                        route=resolved_route,
                        error_code=data.get("error_code", "summary_failed"),
                        **follow_meta,
                    )
                if data:
                    data["ok"] = True
                    data.update({k: v for k, v in follow_meta.items() if v not in (None, "", [])})
                    _clear_sticky_route(sticky_key)
                    return _gateway_success(data, resolved_route=resolved_route)
                return _gateway_error(
                    "No page summary available after following the selected result.",
                    target_type=target_type,
                    route=resolved_route,
                    error_code="summary_empty",
                    **follow_meta,
                )

            if target_type == "structured_data":
                await _emit_web_progress(f"Extracting data from {chosen_domain}…", "browsing")
                raw = await tool_registry.execute(
                    "extract_structured_data",
                    {"item_type": item_hint, "query": query, "max_items": max_items},
                )
                data = _safe_json(raw)
                if data.get("ok") is False:
                    return _gateway_error(
                        data.get("message", "structured extraction failed"),
                        target_type=target_type,
                        route=resolved_route,
                        error_code=data.get("error_code", "structured_extract_failed"),
                        item_hint=item_hint,
                        **follow_meta,
                    )
                if data:
                    data["ok"] = True
                    data["item_count"] = int(data.get("item_count", len(data.get("items", [])) or 0))
                    if isinstance(data.get("items"), list):
                        preview = _items_preview(data.get("items", []), max_chars)
                        if preview:
                            data["content"] = preview
                            data["content_length"] = len(preview)
                    data.update({k: v for k, v in follow_meta.items() if v not in (None, "", [])})
                    _clear_sticky_route(sticky_key)
                    return _gateway_success(data, resolved_route=resolved_route)
                return _gateway_error(
                    "No structured data extracted after following the selected result.",
                    target_type=target_type,
                    route=resolved_route,
                    error_code="structured_empty",
                    item_hint=item_hint,
                    **follow_meta,
                )

            await _emit_web_progress(f"Reading {chosen_domain}…", "browsing")
            raw = await tool_registry.execute(
                "read_page_content",
                {"max_chars": max_chars, "scroll_pages": 2},
            )
            data = _safe_json(raw)
            if data.get("ok") is False:
                return _gateway_error(
                    data.get("message", "page read failed"),
                    target_type=target_type,
                    route=resolved_route,
                    error_code=data.get("error_code", "page_read_failed"),
                    **follow_meta,
                )
            if data:
                data["ok"] = True
                data.update({k: v for k, v in follow_meta.items() if v not in (None, "", [])})
                _clear_sticky_route(sticky_key)
                return _gateway_success(data, resolved_route=resolved_route)
            return _gateway_error(
                "No page content available after following the selected result.",
                target_type=target_type,
                route=resolved_route,
                error_code="page_content_empty",
                **follow_meta,
            )

        return await _background_page_payload(
            target_url=chosen_url,
            resolved_route=resolved_route,
            follow_meta=follow_meta,
        )

    if route == "browser_aci":
        if target_type == "search_results":
            return await _browser_search_results_payload()

        if not url and query:
            return await _search_follow_then_read(resolved_route=route)

        target_session_id = ""
        if url:
            target_session_id = await _browser_open_if_needed(url)

        page_label = _domain(url or str(context.get("browser_url", "")) or "") or "page"

        if target_type == "page_summary":
            await _emit_web_progress(f"Analyzing {page_label}…", "browsing")
            raw = await tool_registry.execute("get_page_summary", {"session_id": target_session_id})
            data = _safe_json(raw)
            if data.get("ok") is False:
                return _gateway_error(
                    data.get("message", "page summary failed"),
                    target_type=target_type,
                    route=route,
                    error_code=data.get("error_code", "summary_failed"),
                )
            if data:
                data["ok"] = True
                _clear_sticky_route(sticky_key)
                return _gateway_success(data, resolved_route=route)
            return _gateway_error("No page summary available", target_type=target_type, route=route)

        if target_type == "structured_data":
            await _emit_web_progress(f"Extracting data from {page_label}…", "browsing")
            raw = await tool_registry.execute(
                "extract_structured_data",
                {"item_type": item_hint, "query": query, "max_items": max_items, "session_id": target_session_id},
            )
            data = _safe_json(raw)
            if data.get("ok") is False:
                return _gateway_error(
                    data.get("message", "structured extraction failed"),
                    target_type=target_type,
                    route=route,
                    error_code=data.get("error_code", "structured_extract_failed"),
                    item_hint=item_hint,
                )
            if data:
                data["ok"] = True
                data["item_count"] = int(data.get("item_count", len(data.get("items", [])) or 0))
                if isinstance(data.get("items"), list):
                    preview = _items_preview(data.get("items", []), max_chars)
                    if preview:
                        data["content"] = preview
                        data["content_length"] = len(preview)
                _clear_sticky_route(sticky_key)
                return _gateway_success(data, resolved_route=route)
            return _gateway_error("No structured data extracted", target_type=target_type, route=route, item_hint=item_hint)

        await _emit_web_progress(f"Reading {page_label}…", "browsing")
        raw = await tool_registry.execute(
            "read_page_content",
            {"max_chars": max_chars, "scroll_pages": 2, "session_id": target_session_id},
        )
        data = _safe_json(raw)
        if data.get("ok") is False:
            return _gateway_error(
                data.get("message", "page read failed"),
                target_type="page_content",
                route=route,
                error_code=data.get("error_code", "page_read_failed"),
            )
        content_length = int(data.get("content_length", 0) or 0)
        if content_length > 0:
            data["ok"] = True
            _clear_sticky_route(sticky_key)
            return _gateway_success(data, resolved_route=route)

        fallback = await tool_registry.execute("browser_read_page", {"query": query, "refresh": True, "session_id": target_session_id})
        fb = _safe_json(fallback)
        if fb:
            fb["ok"] = True
            _clear_sticky_route(sticky_key)
            return _gateway_success(fb, resolved_route=route)
        return _gateway_error("No readable page content extracted", target_type="page_content", route=route)

    if target_type == "search_results":
        return await _background_search_payload(resolved_route=route)

    if not url and query:
        return await _search_follow_then_read(resolved_route=route)

    return await _background_page_payload(target_url=url.strip(), resolved_route=route)


# ═══════════════════════════════════════════════════════════════
#  Tool Categories
# ═══════════════════════════════════════════════════════════════

class ToolCategories:
    """Tool categorization for intelligent selection."""
    
    # Always available - communication tools
    CORE = {"send_response", "await_reply"}
    
    # Perception tools - for understanding state (observational only)
    PERCEPTION = {"read_screen", "get_ui_tree", "read_file"}
    
    # Smart interaction — high-level tools that use Accessibility API internally.
    # These should be PREFERRED over raw click_element + read_screen for UI actions.
    SMART_INTERACTION = {"click_ui", "type_in_field"}
    
    # Browser DOM tools — extension-backed browser refs should be the default path.
    BROWSER_DOM = {
        "browser_snapshot",
        "browser_read_page",
        "browser_read_text",
        "browser_find",
        "browser_click_match",
        "browser_describe_ref",
        "browser_click_ref",
        "browser_type_ref",
        "browser_select_ref",
        "browser_refresh_refs",
        "browser_scroll",
        "browser_wait_for",
        "browser_assert",
        "browser_list_tabs",
        "browser_switch_tab",
    }

    # Legacy selector-based browser tools — use only when the user explicitly asks for CSS/XPath selectors.
    BROWSER_LEGACY = {"browser_click", "browser_fill"}
    
    # App control - launching/closing applications
    # `get_running_apps` belonged to no category, so the selector could never
    # offer it — the planner only ever saw `run_shell` for "what's running",
    # and reached for a shell command (approval-gated, and AppleScript on a
    # Windows box). It is read-only and cross-platform, so it belongs here.
    APP_CONTROL = {"open_app", "quit_app", "close_window", "open_url", "get_running_apps"}
    
    # UI automation - low-level clicking, typing, shortcuts (use SMART_INTERACTION first)
    UI_AUTOMATION = {"click_element", "hover_element", "type_text", "press_key", "run_shortcut", "mouse_action"}
    
    # Media control
    MEDIA = {"play_media"}
    
    # File system - terminal and file operations
    FILE_SYSTEM = {"run_shell", "read_file", "write_file", "replace_in_file", "list_directory"}
    
    # Window management
    WINDOW = {"window_manager", "close_window"}
    
    # Clipboard
    CLIPBOARD = {"clipboard_ops"}
    
    # Image tools — downloading, copying, capturing images
    IMAGE = {"save_image", "copy_image_to_clipboard", "capture_region_screenshot", "browser_copy_image"}
    
    # Google Workspace — direct API tools for Docs, Sheets, Slides, Drive, Gmail, Calendar
    GOOGLE_WORKSPACE = {
        "gdocs_create", "gdocs_read", "gdocs_append", "gdocs_insert_image",
        "gsheets_create", "gsheets_read", "gsheets_write", "gsheets_append_rows", "gsheets_formula",
        "gslides_create", "gslides_add_slide",
        "gdrive_search", "gdrive_upload",
        "gmail_send", "gmail_read", "gmail_draft",
        "gcal_create_event", "gcal_list_events",
        "gworkspace_analyze",
    }

    # Gateway abstractions — preferred LLM-facing tools for complex surfaces
    GATEWAY = {
        "get_web_information",
    }

    # Research workflows — force browser read tools + synthesis/output tools.
    RESEARCH = {
        "open_url", "get_web_information",
        "browser_click_ref", "browser_type_ref", "browser_select_ref",
        "browser_list_tabs", "browser_switch_tab", "browser_refresh_refs",
        "gworkspace_analyze", "gdocs_create", "gdocs_append", "write_file", "read_file",
        "remember_this", "recall_memory",
    }

    # Vault memory — permanent cross-session storage
    VAULT = {"remember_this", "recall_memory", "forget_this"}

    # Messaging — visual message sending
    MESSAGING = {"send_imessage"}

    # Form filling — vault-powered form automation
    FORM = {"fill_form"}

    # Phone voice agents (Bolna) — outbound calls
    VOICE_CALL = {"bolna_call", "bolna_call_status"}
    
    # All tools combined
    ALL = CORE | PERCEPTION | SMART_INTERACTION | BROWSER_DOM | BROWSER_LEGACY | APP_CONTROL | UI_AUTOMATION | MEDIA | FILE_SYSTEM | WINDOW | CLIPBOARD | IMAGE | GOOGLE_WORKSPACE | GATEWAY | RESEARCH | VAULT | MESSAGING | FORM | VOICE_CALL


CORE_PRIORITY = ["send_response", "await_reply"]
APP_CONTROL_PRIORITY = ["open_url", "open_app", "close_window", "quit_app"]
FILE_SYSTEM_PRIORITY = ["list_directory", "read_file", "run_shell", "write_file", "replace_in_file"]
COMMUNICATION_PRIORITY = [
    "open_app",
    "click_ui",
    "type_in_field",
    "type_text",
    "press_key",
    "run_shortcut",
    "get_ui_tree",
    "open_url",
]
BROWSER_DOM_PRIORITY = [
    "browser_snapshot",
    "browser_read_page",
    "browser_click_ref",
    "browser_type_ref",
    "browser_select_ref",
    "browser_click_match",
    "browser_find",
    "browser_describe_ref",
    "browser_scroll",
    "browser_refresh_refs",
    "browser_list_tabs",
    "browser_switch_tab",
    "browser_wait_for",
    "browser_assert",
]
CHROME_ACTION_PRIORITY = [
    "press_key",
    "run_shortcut",
    "click_ui",
    "type_in_field",
    "get_ui_tree",
    "click_element",
    "hover_element",
    "mouse_action",
]

GOOGLE_WORKSPACE_PRIORITY = [
    "gdocs_create", "gdocs_read", "gdocs_append", "gdocs_insert_image",
    "gsheets_create", "gsheets_read", "gsheets_write", "gsheets_append_rows", "gsheets_formula",
    "gslides_create", "gslides_add_slide",
    "gdrive_search", "gdrive_upload",
    "gmail_send", "gmail_read", "gmail_draft",
    "gcal_create_event", "gcal_list_events",
    "gworkspace_analyze",
]

RESEARCH_PRIORITY = [
    "open_url",
    "get_web_information",
    "browser_scroll",
    "gworkspace_analyze",
    "gdocs_create",
    "gdocs_append",
    "write_file",
    "read_file",
]

GATEWAY_PRIORITY = [
    "get_web_information",
]


# ═══════════════════════════════════════════════════════════════
#  Keyword Triggers
# ═══════════════════════════════════════════════════════════════

# Keywords that trigger each tool category
CATEGORY_TRIGGERS: Dict[str, List[str]] = {
    "APP_CONTROL": [
        "open", "launch", "start", "run", "close", "quit", "exit", "kill",
        "spotify", "chrome", "safari", "slack", "discord", "code", "cursor",
        "terminal", "finder", "mail", "messages", "notes", "app",
        # "what is running" style requests — these must reach get_running_apps
        # rather than falling through to the shell.
        "running", "application", "applications", "task manager", "processes"
    ],
    "SMART_INTERACTION": [
        "click", "tap", "press", "button", "type", "enter", "input",
        "field", "checkbox", "menu", "select", "toggle", "fill",
        "submit", "login", "sign in", "search bar"
    ],
    "BROWSER_DOM": [
        "form", "submit", "web page", "webpage", "html", "dom",
        "browser", "tab", "website", "gmail", "youtube", "google",
        "scroll down", "scroll up", "scroll page", "scroll the page",
        "scroll to", "next page", "more results", "load more"
    ],
    "BROWSER_LEGACY": [
        "selector", "css", "xpath", "queryselector", "dom selector"
    ],
    "UI_AUTOMATION": [
        "shortcut", "key", "hover", "mouse", "cursor", "scroll",
        "drag", "coordinates"
    ],
    "PERCEPTION": [
        "screen", "see", "look", "what's on", "show me", "read", "ui",
        "analyze", "describe", "elements", "window"
    ],
    "VISION_ONLY": [
        "what's on my screen", "what do you see", "describe the screen",
        "screenshot", "show me what", "read the screen", "what is on"
    ],
    "FILE_SYSTEM": [
        "file", "folder", "directory", "list", "create", "write", "save", "delete",
        "edit", "modify", "replace",
        "terminal", "command", "shell", "bash", "script", "git", "npm",
        "pip", "brew", "install", "run"
    ],
    "VOICE_CALL": [
        "call ", "phone", "dial", "telephone", "voice agent", "bolna",
        "call karo", "phone karo", "फोन", "कॉल",
    ],
    "MEDIA": [
        "play", "pause", "stop", "resume", "skip", "next", "previous",
        "music", "song", "video", "media", "volume"
    ],
    "WINDOW": [
        "window", "minimize", "maximize", "fullscreen", "resize", "move"
    ],
    "CLIPBOARD": [
        "clipboard", "copy", "paste", "cut"
    ],
    "IMAGE": [
        "image", "picture", "photo", "screenshot", "capture",
        "copy image", "paste image", "save image", "download image",
        "insert image", "grab image", "copy the image", "copy that image",
        "get the image", "copy photo", "save photo", "logo",
        "copy the picture", "take screenshot", "region screenshot",
        "capture screen", "capture region"
    ],
    "WEB": [
        "youtube", "google", "gmail", "github", "twitter", "reddit",
        "website", "url", "http", "www", "search", "browse"
    ],
    "GOOGLE_WORKSPACE": [
        "google doc", "google sheet", "google slide", "google drive",
        "google calendar", "gmail", "spreadsheet", "presentation",
        "gdoc", "gsheet", "gslide", "gdrive", "gcal",
        "docs.google", "sheets.google", "slides.google", "drive.google",
        "calendar event", "create a doc", "create a sheet", "create a slide",
        "write a document", "make a spreadsheet", "make a presentation",
        "create a document", "make a document", "new document",
        "write a doc", "create document", "create doc",
        "document it", "document this", "document all", "document the",
        "send email", "send an email", "draft email", "draft an email",
        "read my email", "check email", "check my email", "email",
        "upload to drive", "upload to google", "upload file", "upload this file",
        "save to drive", "put on drive", "search drive", "find in drive", "my drive",
        "add rows", "append rows", "write rows", "fill spreadsheet",
        "add row", "new row", "insert row", "more rows",
        "create event", "schedule meeting", "schedule a meeting", "my calendar", "upcoming events",
        "schedule", "meeting", "appointment", "agenda",
        "analyse document", "analyze document", "summarise document", "summarize document",
        "read the doc", "read the sheet", "what's in this doc",
        "summarise this", "summarize this", "analyse this", "analyze this",
        "summarise it", "summarize it", "what does this document",
        "essay", "report", "letter", "memo",
        "docs.new", "sheets.new", "slides.new"
    ],
    "RESEARCH": [
        "research", "investigate", "analyze", "analyse", "compare", "study",
        "scrape", "extract data",
        "find the best", "best", "top", "pros and cons", "tradeoff", "trade-off",
        "summarize findings", "summary report", "write a report", "write a document",
        "write up", "brief", "comparison", "ranked", "ranking", "evidence"
    ]
}


# ═══════════════════════════════════════════════════════════════
#  Tool Selector Class
# ═══════════════════════════════════════════════════════════════

class ToolSelector:
    """
    Selects relevant tools based on user request and context.
    Reduces cognitive load on the LLM by filtering to relevant tools.
    """
    
    def __init__(self, tool_registry=None):
        """
        Initialize the tool selector.
        
        Args:
            tool_registry: Optional tool registry to get available tool names
        """
        self.tool_registry = tool_registry
        self._all_tool_names: Optional[Set[str]] = None

    def _get_all_tools(self) -> Set[str]:
        """Get all available tool names from registry."""
        if self._all_tool_names is not None:
            return self._all_tool_names
        
        if self.tool_registry:
            self._all_tool_names = set(self.tool_registry.list_names())
        else:
            self._all_tool_names = ToolCategories.ALL
        
        return self._all_tool_names

    def _should_abstract_web_information(
        self,
        combined_text: str,
        matched_categories: Set[str],
        is_browser_interaction: bool,
        has_explicit_source: bool = False,
    ) -> bool:
        if is_browser_interaction:
            return False
        if "RESEARCH" in matched_categories:
            return True
        info_markers = (
            "search results",
            "research",
            "investigate",
            "summarize",
            "analyse",
            "analyze",
            "compare",
            "look up",
            "find sources",
            "article",
            "read this page",
            "extract information",
            "overview",
            "summary",
            "what do i need to do",
            "what i need to do",
            "responsibilities",
            "tasks",
            "brief",
        )
        if any(marker in combined_text for marker in info_markers):
            return True
        if has_explicit_source and any(
            marker in combined_text for marker in ("overview", "summary", "tasks", "responsibilities", "brief")
        ):
            return True
        return False

    # Explicit launch intent, e.g. "open chrome and summarise it".
    _LAUNCH_VERBS = re.compile(
        r"\b(?:open|launch|start|run|boot)\b", re.IGNORECASE)

    # "open" is also an adjective. "the Gmail webpage currently open" describes
    # existing state, but matched _LAUNCH_VERBS, so open_app stayed on offer for
    # a request that explicitly said the page was already there — leaving the
    # planner free to launch a second browser instead of reading the first.
    _STATE_OPEN = re.compile(
        r"\b(?:currently|already|now|still|is|are|was|were|that'?s|it'?s|"
        r"the|this|that|an?)\s+open\b", re.IGNORECASE)

    _EDITOR_APPS = {
        "visual studio code", "vscode", "code", "vscodium", "cursor",
        "visual studio", "sublime text", "notepad++", "pycharm",
        "intellij idea", "webstorm", "neovim", "vim", "emacs",
    }

    # Words meaning "the source I am looking at", as opposed to a web page.
    _CODE_WORDS = (
        "code", "function", "method", "class", "algorithm", "complexity",
        "bug", "file", "script", "variable", "refactor", "syntax",
        "implementation", "snippet",
    )

    @classmethod
    def _is_editor_app(cls, context_app: str) -> bool:
        return (context_app or "").strip().lower() in cls._EDITOR_APPS

    @classmethod
    def _looks_like_code_request(cls, user_request: str) -> bool:
        low = " ".join((user_request or "").lower().split())
        if any(word in low for word in cls._CODE_WORDS):
            return True
        return any(name in low for name in ("vs code", "vscode", "visual studio code", "editor"))

    @staticmethod
    def _is_browser_app(context_app: str) -> bool:
        app = (context_app or "").strip().lower()
        return bool(app) and app in {
            "google chrome", "chrome", "brave browser", "brave", "microsoft edge",
            "edge", "firefox", "mozilla firefox", "chromium", "safari", "arc",
        }

    @classmethod
    def _asks_to_launch(cls, user_request: str) -> bool:
        """Explicit launch intent — then open_app stays on the table."""
        text = cls._STATE_OPEN.sub(" ", user_request or "")
        return bool(cls._LAUNCH_VERBS.search(text))

    @staticmethod
    def _looks_like_inspection(user_request: str) -> bool:
        """Is the user asking to look at something that already exists?"""
        low = " ".join((user_request or "").lower().split())
        return any(v in low for v in _INSPECTION_VERBS)

    def select(
        self,
        user_request: str,
        context_app: str = "",
        context_url: str = "",
        conversation_history: str = "",
        clipboard_content: str = "",
        selected_text: str = "",
        max_tools: int = 12,
        intent_action: str = "",
        intent_target_type: str = "",
        intent_target_value: str = "",
    ) -> List[str]:
        """
        Select relevant tools for the given request.
        
        Args:
            user_request: The user's text request
            context_app: Current active application
            context_url: Current browser URL (if any)
            conversation_history: Recent conversation context
            max_tools: Maximum number of tools to return
            
        Returns:
            List of relevant tool names
        """
        if _looks_like_generic_media_open(
            user_request,
            intent_action=intent_action,
            intent_target_type=intent_target_type,
            intent_target_value=intent_target_value,
        ):
            media_tools = [
                tool
                for tool in ("send_response", "await_reply", "play_media", "open_url")
                if tool in self._get_all_tools()
            ]
            print(f"[ToolSelector] Selected {len(media_tools)} tools: {media_tools}")
            return media_tools

        if _looks_like_direct_desktop_message(
            user_request,
            context_app=context_app,
            context_url=context_url,
            clipboard_content=clipboard_content,
            selected_text=selected_text,
            intent_action=intent_action,
            intent_target_value=intent_target_value,
        ):
            message_tools = [
                tool
                for tool in _DIRECT_MESSAGE_TOOLS
                if tool in self._get_all_tools()
            ]
            print(f"[ToolSelector] Selected {len(message_tools)} tools: {message_tools}")
            return message_tools

        contextual_text = [conversation_history, clipboard_content, selected_text]
        if _norm_text(intent_action) in {"open", "play"} and not _has_deictic_source_reference(user_request):
            contextual_text = []

        # Combine all text for analysis
        combined_text = " ".join(
            part for part in (
                user_request,
                context_app,
                context_url,
                *contextual_text,
            )
            if part
        ).lower()
        has_explicit_source = any(
            str(value or "").strip().lower().startswith(("http://", "https://"))
            for value in (context_url, *contextual_text)
        )
        
        # Start with core tools (always available)
        selected: Set[str] = set(ToolCategories.CORE)
        
        # Check each category's triggers
        matched_categories: Set[str] = set()
        
        for category, triggers in CATEGORY_TRIGGERS.items():
            if any(trigger in combined_text for trigger in triggers):
                matched_categories.add(category)
        
        # ── Smart routing: prefer accessibility-based tools over vision ──
        # When the user wants to INTERACT (click, type, fill), give them the
        # high-level accessibility tools (click_ui, type_in_field) and the
        # low-level fallbacks (click_element, type_text) but NOT read_screen.
        # read_screen is only included for purely OBSERVATIONAL requests.
        is_interaction = "SMART_INTERACTION" in matched_categories
        is_observation_only = "VISION_ONLY" in matched_categories
        is_browser_context = any(b in combined_text for b in [
            "chrome", "safari", "arc", "brave", "firefox", "browser",
            "web page", "webpage", "website"
        ]) or bool(context_url)
        browser_chrome_action = is_browser_context and is_browser_chrome_action(combined_text)
        is_mixed_local_workflow = _is_mixed_local_workflow(combined_text, context_app)
        is_browser_interaction = is_browser_context and not is_observation_only and not browser_chrome_action and any(trigger in combined_text for trigger in [
            "youtube", "gmail", "google", "video", "play", "search", "result", "link",
            "button", "input", "field", "click", "type", "select", "form", "submit"
        ])
        if "RESEARCH" in matched_categories:
            is_browser_interaction = False
        
        # Add tools from matched categories
        for category in matched_categories:
            if category == "APP_CONTROL":
                selected.update(ToolCategories.APP_CONTROL)
            elif category == "SMART_INTERACTION":
                # Always prefer high-level tools; include low-level as fallback
                selected.update(ToolCategories.SMART_INTERACTION)
                selected.update(ToolCategories.UI_AUTOMATION)
                # Include get_ui_tree for manual inspection if needed
                selected.add("get_ui_tree")
                # Add browser DOM tools when in browser context
                if is_browser_context:
                    selected.update(ToolCategories.BROWSER_DOM)
            elif category == "BROWSER_DOM":
                selected.update(ToolCategories.BROWSER_DOM)
                selected.update(ToolCategories.SMART_INTERACTION)
            elif category == "BROWSER_LEGACY":
                selected.update(ToolCategories.BROWSER_DOM)
                selected.update(ToolCategories.BROWSER_LEGACY)
                selected.update(ToolCategories.SMART_INTERACTION)
            elif category == "UI_AUTOMATION":
                selected.update(ToolCategories.UI_AUTOMATION)
                selected.update(ToolCategories.SMART_INTERACTION)
            elif category == "PERCEPTION":
                # For observational requests: include read_screen
                # For interaction requests: only include get_ui_tree, not read_screen
                if is_interaction and not is_observation_only:
                    selected.add("get_ui_tree")
                else:
                    selected.update(ToolCategories.PERCEPTION)
            elif category == "VISION_ONLY":
                selected.update(ToolCategories.PERCEPTION)
            elif category == "FILE_SYSTEM":
                selected.update(ToolCategories.FILE_SYSTEM)
            elif category == "VOICE_CALL":
                selected.update(ToolCategories.VOICE_CALL)
            elif category == "MEDIA":
                selected.update(ToolCategories.MEDIA)
            elif category == "WINDOW":
                selected.update(ToolCategories.WINDOW)
            elif category == "CLIPBOARD":
                selected.update(ToolCategories.CLIPBOARD)
            elif category == "IMAGE":
                selected.update(ToolCategories.IMAGE)
                selected.update(ToolCategories.CLIPBOARD)  # image tools often need clipboard_ops(paste)
                if is_browser_context:
                    selected.update(ToolCategories.BROWSER_DOM)
            elif category == "GOOGLE_WORKSPACE":
                selected.update(ToolCategories.GOOGLE_WORKSPACE)
                selected.add("open_url")
            elif category == "RESEARCH":
                selected.update(ToolCategories.RESEARCH)
                selected.update(ToolCategories.BROWSER_DOM)
                selected.update({"gdocs_create", "gdocs_append", "gworkspace_analyze"})
            elif category == "WEB":
                selected.add("open_url")

        if browser_chrome_action:
            selected.difference_update(ToolCategories.BROWSER_DOM)
            selected.difference_update(ToolCategories.BROWSER_LEGACY)
            selected.update(ToolCategories.UI_AUTOMATION)
            selected.update(ToolCategories.SMART_INTERACTION)
            selected.add("get_ui_tree")

        # Auto-inject Google Workspace tools when context URL is a Workspace domain
        _gw_auto_domains = ("docs.google.com", "sheets.google.com", "slides.google.com",
                            "drive.google.com", "mail.google.com", "calendar.google.com",
                            "docs.new", "sheets.new", "slides.new")
        if context_url and any(d in context_url.lower() for d in _gw_auto_domains):
            selected.update(ToolCategories.GOOGLE_WORKSPACE)
            matched_categories.add("GOOGLE_WORKSPACE")

        if "RESEARCH" in matched_categories:
            non_doc_workspace_terms = ("sheet", "spreadsheet", "slide", "presentation", "email", "gmail", "calendar", "drive", "upload")
            wants_non_doc_workspace = any(term in combined_text for term in non_doc_workspace_terms)
            if not wants_non_doc_workspace:
                selected.difference_update({
                    "gsheets_create", "gsheets_read", "gsheets_write", "gsheets_append_rows", "gsheets_formula",
                    "gslides_create", "gslides_add_slide",
                    "gdrive_search", "gdrive_upload",
                    "gmail_send", "gmail_read", "gmail_draft",
                    "gcal_create_event", "gcal_list_events",
                })

        if is_mixed_local_workflow:
            matched_categories.add("FILE_SYSTEM")
            selected.update(ToolCategories.FILE_SYSTEM)
            selected.update(ToolCategories.APP_CONTROL)
            selected.update(ToolCategories.SMART_INTERACTION)
            max_tools = max(max_tools, 18)

        if self._should_abstract_web_information(
            combined_text,
            matched_categories,
            is_browser_interaction,
            has_explicit_source=has_explicit_source,
        ):
            selected.add("get_web_information")
            selected.difference_update(_ABSTRACT_WEB_INFO_TOOLS)
            matched_categories.add("GATEWAY")

        # If nothing matched beyond core, add common tools
        if len(selected) <= len(ToolCategories.CORE):
            # Default to app control + smart interaction (most common use case)
            selected.update(ToolCategories.APP_CONTROL)
            selected.update(ToolCategories.SMART_INTERACTION)
            selected.add("run_shell")
        
        # Filter to only tools that exist in registry
        available_tools = self._get_all_tools()
        selected = selected.intersection(available_tools)

        if _norm_text(intent_action) == "communicate":
            selected.update(
                tool
                for tool in (
                    "open_app",
                    "click_ui",
                    "type_in_field",
                    "type_text",
                    "press_key",
                    "run_shortcut",
                    "get_ui_tree",
                )
                if tool in available_tools
            )

        if is_browser_interaction:
            # Browser interaction plans need enough room to keep the
            # ref-based tools (click/type/select) and read/scroll tools.
            max_tools = max(max_tools, 16)
            selected.update(ToolCategories.BROWSER_DOM)
            selected.difference_update({
                "read_screen",
                "get_ui_tree",
                "click_element",
                "hover_element",
                "click_ui",
                "type_in_field",
                "type_text",
            })
            # Google Workspace hybrid: keep OS typing tools for canvas-based editors
            _gw_domains = ("docs.google.com", "sheets.google.com", "slides.google.com", "docs.new")
            _gw_combined = ((context_url or "") + " " + combined_text).lower()
            if any(d in _gw_combined for d in _gw_domains) or any(
                h in _gw_combined for h in ("google doc", "google sheet", "google slide", "spreadsheet")
            ):
                selected.update({"type_text", "press_key", "run_shortcut", "clipboard_ops", "read_screen"})
                # Also inject the dedicated Google Workspace API tools so the
                # agent can use direct API calls instead of browser clicking.
                if "RESEARCH" in matched_categories:
                    selected.update({"gdocs_create", "gdocs_append", "gdocs_read", "gworkspace_analyze"})
                else:
                    selected.update(ToolCategories.GOOGLE_WORKSPACE)

        # Boost limit when Google Workspace is active (many specialised tools)
        if "GOOGLE_WORKSPACE" in matched_categories:
            max_tools = max(max_tools, 25)
        if "RESEARCH" in matched_categories:
            max_tools = max(max_tools, 22)

        # Ensure we don't exceed max_tools (prioritize core and app_control)
        if len(selected) > max_tools:
            # Priority order: CORE > APP_CONTROL > matched categories
            priority_tools = list(CORE_PRIORITY)
            priority_tools.extend(APP_CONTROL_PRIORITY)
            if is_mixed_local_workflow:
                priority_tools.extend(FILE_SYSTEM_PRIORITY)
            if _norm_text(intent_action) == "communicate":
                priority_tools.extend(COMMUNICATION_PRIORITY)
            if "RESEARCH" in matched_categories:
                priority_tools.extend(RESEARCH_PRIORITY)
            if "GATEWAY" in matched_categories:
                priority_tools.extend(GATEWAY_PRIORITY)
            if "GOOGLE_WORKSPACE" in matched_categories:
                priority_tools.extend(GOOGLE_WORKSPACE_PRIORITY)
            if is_browser_interaction:
                priority_tools.extend(BROWSER_DOM_PRIORITY)
            if browser_chrome_action:
                priority_tools.extend(CHROME_ACTION_PRIORITY)
            
            final_set = set()
            for tool in priority_tools:
                if tool in selected:
                    final_set.add(tool)
                    if len(final_set) >= max_tools:
                        break
            
            # Fill remaining slots with other selected tools
            for tool in selected:
                if len(final_set) >= max_tools:
                    break
                if tool not in final_set:
                    final_set.add(tool)
                    if len(final_set) >= max_tools:
                        break
            
            selected = final_set
        
        # Inspection requests must be able to LOOK at the app that is already
        # open. Without these the only app-shaped tool on offer was open_app,
        # so "summarise what's open in VS Code" could only be answered by
        # launching a second VS Code window. Adding the window-inspection tools
        # gives the planner a correct option; open_app stays available for
        # genuine launch requests.
        if self._looks_like_inspection(user_request) and (context_app or context_url):
            for tool in _INSPECTION_TOOLS:
                if tool in self._all_tool_names and tool not in selected:
                    selected = list(selected) + [tool] if isinstance(selected, list) else set(selected) | {tool}

        # Reading a page in a browser that is ALREADY open must not be done by
        # launching it. Measured: the executor picked open_app first "to bring
        # Chrome to the foreground", which surfaced Chrome's profile chooser,
        # and the next step then read "Who's using Chrome?" as if it were the
        # page. Withhold open_app for browser inspection unless the user
        # actually asked to open something.
        if (self._looks_like_inspection(user_request)
                and (context_app or context_url)
                and not self._asks_to_launch(user_request)):
            # Widened from browsers to any identified foreground app: asking to
            # "summarise the current VS Code window" was still offering
            # open_app, and the executor launched a *second* VS Code instead of
            # reading the one already in front.
            selected = [t for t in selected if t != "open_app"] if isinstance(selected, list) else {t for t in selected if t != "open_app"}

        # Code/editor questions about an editor that is already open must be
        # able to reach the registered editor reader. It was in the live
        # registry but never offered, so planning fell back to screenshots, UI
        # Automation, or asking the user to paste their code.
        if (self._is_editor_app(context_app)
                and self._looks_like_code_request(user_request)
                and "read_active_editor_file" in self._all_tool_names
                and "read_active_editor_file" not in selected):
            selected = (list(selected) + ["read_active_editor_file"]) if isinstance(selected, list) else (set(selected) | {"read_active_editor_file"})

        # Content questions about a page that is already open in a browser must
        # be able to reach the Browser Bridge. Keyed on the foreground app and
        # URL rather than on any site name, so this is not a Gmail special case.
        browser_context = self._is_browser_app(context_app) or str(context_url or "").startswith("http")
        prefer_browser: list[str] = []
        if self._looks_like_inspection(user_request) and browser_context:
            for tool in _BROWSER_READ_TOOLS:
                if tool in self._all_tool_names:
                    if tool not in selected:
                        selected = (list(selected) + [tool]) if isinstance(selected, list) else (set(selected) | {tool})
                    prefer_browser.append(tool)

        result = list(selected)
        if prefer_browser:
            # Order is the only preference signal the planner prompt carries:
            # DOM reads first, screenshots after, so OCR is the fallback rather
            # than the first thing tried.
            rest = [t for t in result if t not in prefer_browser]
            result = prefer_browser + rest
        print(f"[ToolSelector] Selected {len(result)} tools: {result}")
        return result

    def format_planning_tool_summary(self, tool_names: Optional[List[str]] = None) -> str:
        """Format the filtered LLM-facing tool surface for milestone planning."""
        declarations = []
        if tool_names is not None:
            declarations = self.get_llm_tool_declarations(tool_names)
        elif self.tool_registry:
            declarations = self.get_llm_tool_declarations(self.tool_registry.list_names())

        if not declarations and tool_names is None and self.tool_registry:
            declarations = self.tool_registry.declarations()
        if not declarations:
            return "  (no tools available)"

        lines: list[str] = []
        for decl in declarations:
            name = decl["name"]
            desc = str(decl.get("description", "")).strip()
            params = decl.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            required = set(params.get("required", [])) if isinstance(params, dict) else set()
            param_parts: list[str] = []
            for pname, pschema in props.items():
                if pname == "reasoning":
                    continue
                marker = "*" if pname in required else ""
                ptype = "string"
                if isinstance(pschema, dict):
                    enum_vals = pschema.get("enum")
                    if isinstance(enum_vals, list) and enum_vals:
                        ptype = "{" + "|".join(str(v) for v in enum_vals[:6]) + "}"
                    else:
                        ptype = pschema.get("type", "string")
                param_parts.append(f"{pname}{marker}:{ptype}")
            param_str = ", ".join(param_parts) if param_parts else "(no args)"
            short_desc = desc[:140] + "…" if len(desc) > 140 else desc
            lines.append(f"- {name}({param_str}) — {short_desc}")
        return "\n".join(lines)

    def get_llm_tool_declarations(self, tool_names: List[str]) -> List[dict]:
        """Return the filtered LLM-facing tool surface for a request."""
        if not self.tool_registry:
            return []

        allowed = set(tool_names or [])
        if allowed.intersection(_ABSTRACT_WEB_INFO_TOOLS) or "get_web_information" in allowed:
            allowed.add("get_web_information")
            allowed.difference_update(_ABSTRACT_WEB_INFO_TOOLS)

        declarations = []
        for decl in self.tool_registry.declarations():
            if decl["name"] in allowed:
                declarations.append(decl)
        return declarations

    def select_for_intent(
        self,
        action: str,
        target_type: str,
        target_value: str = ""
    ) -> List[str]:
        """
        Select tools based on parsed intent (faster than keyword matching).
        
        Args:
            action: IntentAction value (e.g., "open", "close", "search")
            target_type: TargetType value (e.g., "app", "url", "file")
            target_value: The specific target
            
        Returns:
            List of relevant tool names
        """
        selected: Set[str] = set(ToolCategories.CORE)
        
        # Map actions to tool categories
        # Prefer SMART_INTERACTION over raw UI_AUTOMATION for click/type actions
        action_map = {
            "open": ToolCategories.APP_CONTROL,
            "close": ToolCategories.APP_CONTROL | ToolCategories.WINDOW,
            "search": {"open_url", "get_web_information"},
            "create": ToolCategories.FILE_SYSTEM | ToolCategories.GOOGLE_WORKSPACE,
            "delete": ToolCategories.FILE_SYSTEM,
            "modify": ToolCategories.FILE_SYSTEM | ToolCategories.SMART_INTERACTION | ToolCategories.UI_AUTOMATION,
            "analyze": ToolCategories.PERCEPTION | {"gworkspace_analyze", "get_web_information"},
            "navigate": ToolCategories.APP_CONTROL | ToolCategories.SMART_INTERACTION,
            "execute": ToolCategories.SMART_INTERACTION | ToolCategories.UI_AUTOMATION | ToolCategories.FILE_SYSTEM,
            "play": ToolCategories.MEDIA,
            "communicate": {
                "open_app",
                "open_url",
                "click_ui",
                "type_in_field",
                "type_text",
                "press_key",
                "run_shortcut",
                "get_ui_tree",
                "gmail_send",
                "gmail_draft",
            },
            "query": ToolCategories.PERCEPTION | {"get_web_information"},
            "click": ToolCategories.SMART_INTERACTION | {"click_element"},
            "type": ToolCategories.SMART_INTERACTION | {"type_text"},
            "fill": ToolCategories.SMART_INTERACTION | ToolCategories.BROWSER_DOM,
        }
        
        if action in action_map:
            selected.update(action_map[action])
        
        # Add target-specific tools
        target_map = {
            "app": ToolCategories.APP_CONTROL,
            "url": {"open_url"} | ToolCategories.BROWSER_DOM,
            "file": ToolCategories.FILE_SYSTEM,
            "folder": ToolCategories.FILE_SYSTEM,
            "ui_element": ToolCategories.SMART_INTERACTION | {"get_ui_tree", "click_element"},
            "web_element": ToolCategories.BROWSER_DOM | ToolCategories.SMART_INTERACTION,
            "system": {"run_shell"},
        }
        
        if target_type in target_map:
            selected.update(target_map[target_type])
        
        # Filter to available tools
        available_tools = self._get_all_tools()
        selected = selected.intersection(available_tools)
        
        return list(selected)

    def get_tool_descriptions(self, tool_names: List[str]) -> str:
        """
        Get formatted descriptions for the selected tools.
        
        Args:
            tool_names: List of tool names to describe
            
        Returns:
            Formatted string with tool descriptions
        """
        if not self.tool_registry:
            return f"Tools: {', '.join(tool_names)}"

        declarations = self.get_llm_tool_declarations(tool_names)
        lines = []
        
        for decl in declarations:
            if decl["name"] in tool_names:
                name = decl["name"]
                desc = decl.get("description", "")[:100]
                params = list(decl.get("parameters", {}).get("properties", {}).keys())
                param_str = ", ".join(params[:3])  # First 3 params
                if len(params) > 3:
                    param_str += ", ..."
                lines.append(f"• {name}({param_str}): {desc}")
        
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Singleton Instance
# ═══════════════════════════════════════════════════════════════

_selector: Optional[ToolSelector] = None


def get_tool_selector(tool_registry=None) -> ToolSelector:
    """Get the global ToolSelector instance."""
    global _selector
    if _selector is None or tool_registry is not None:
        _selector = ToolSelector(tool_registry)
    return _selector
