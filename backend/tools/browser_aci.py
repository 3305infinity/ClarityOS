"""
Moonwalk — Agent-Computer Interface (ACI) Browser Tools
========================================================
High-level compound browser operations for the LLM agent.

These tools abstract away the scroll→snapshot→find→click cycle into
single semantic actions.  The LLM never needs to manually scroll,
refresh snapshots, or resolve ref IDs — the ACI tool handles it.

Raw browser_* primitives remain registered but can be hidden from the
LLM tool list in future; the ACI tools compose them internally.
"""

import asyncio
import json
from typing import Optional, Any
from urllib.parse import urlparse

from browser.interpreter_ai import (
    BrowserInterpretationError,
    extract_structured_items_with_flash,
    summarize_page_with_flash,
)
from browser.selector_ai import select_browser_candidate_with_flash
from browser.bridge import browser_bridge
from browser.models import ActionRequest
from browser.shopping_extractor import extract_shopping_items
from tools.registry import registry

# Re-use the low-level helpers from browser_tools
from tools.browser_tools import (
    _require_snapshot,
    _resolve_element,
    _queue_browser_action,
    _norm,
    _snapshot_health,
    _error_payload,
    _lookup_snapshot,
    _queue_research_highlight,
    browser_scroll,
    browser_read_page,
    browser_read_text,
    browser_refresh_refs,
)


def _queue_scanning_action(
    snapshot,
    *,
    label: str = "AI analyzing page…",
    duration_ms: int = 4000,
    start: bool = True,
    session_id: str = "",
) -> None:
    """Queue a scanning_start or scanning_stop action through the bridge."""
    if not snapshot and not browser_bridge.is_connected():
        return
    try:
        action = "scanning_start" if start else "scanning_stop"
        request = ActionRequest(
            action=action,
            ref_id="",
            session_id=session_id or (getattr(snapshot, "session_id", "") if snapshot else ""),
            metadata={
                "tab_id": getattr(snapshot, "tab_id", "") if snapshot else "",
                "label": label,
                "duration": str(max(0, int(duration_ms or 0))),
            },
        )
        browser_bridge.queue_action(request)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════

_MAX_SCROLL_ATTEMPTS = 6        # Max scrolls before giving up
_SCROLL_SETTLE_S = 0.05         # Reduced — browser_scroll now event-waits for snapshot
_SEARCH_RESULT_HOSTS = frozenset({
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "search.yahoo.com",
    "youtube.com",
})
_SEARCH_UTILITY_LABELS = frozenset({
    "images",
    "videos",
    "video",
    "shopping",
    "maps",
    "news",
    "books",
    "tools",
    "feedback",
    "sign in",
    "more results",
    "next",
    "previous",
    "people also ask",
    "related searches",
    "shorts",
    "live",
    "playlists",
    "channels",
    "filters",
})
_SEARCH_UTILITY_PREFIXES = (
    "about this result",
    "more results",
    "people also ask",
    "search instead for",
)

def _url_domain(url: str) -> str:
    if not url:
        return ""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_search_results_page(url: str) -> bool:
    lowered = (url or "").lower()
    host = _url_domain(lowered)
    if host in {"google.com", "bing.com", "duckduckgo.com", "search.yahoo.com"}:
        return "/search" in lowered or "q=" in lowered
    if host == "youtube.com":
        return "/results" in lowered and "search_query=" in lowered
    return False


def _is_search_shell_href(url: str) -> bool:
    lowered = (url or "").lower()
    host = _url_domain(lowered)
    if host in {"google.com", "bing.com", "duckduckgo.com", "search.yahoo.com"}:
        return "/search" in lowered or "q=" in lowered or "tbm=" in lowered
    if host == "youtube.com":
        return "/results" in lowered
    return False


def _looks_like_search_utility(label: str, href: str) -> bool:
    normalized = _norm(label)
    if not normalized:
        return True
    if normalized in _SEARCH_UTILITY_LABELS:
        return True
    if any(normalized.startswith(prefix) for prefix in _SEARCH_UTILITY_PREFIXES):
        return True
    if "google search" in normalized or "youtube" == normalized:
        return True
    if _is_search_shell_href(href):
        return True
    return False


def _deterministic_search_items(snapshot, *, query: str, max_items: int) -> list[dict[str, Any]]:
    if not snapshot or not _is_search_results_page(getattr(snapshot, "url", "")):
        return []

    items: list[dict[str, Any]] = []
    seen_hrefs: set[str] = set()
    query_terms = [
        term for term in _norm(query).split()
        if len(term) > 2 and term not in {"best", "top", "watch", "video", "videos", "youtube", "right", "now"}
    ]

    for el in snapshot.elements:
        if len(items) >= max(1, min(int(max_items or 20), 20)):
            break
        if not getattr(el, "visible", True):
            continue
        href = str(getattr(el, "href", "") or "").strip()
        if not href or not href.startswith("http"):
            continue
        if href in seen_hrefs:
            continue

        label = (el.primary_label() or "").strip()
        context = str(getattr(el, "context_text", "") or "").strip()
        if len(label) < 8:
            continue
        if _looks_like_search_utility(label, href):
            continue

        haystack = _norm(f"{label} {context} {href}")
        if query_terms and not any(term in haystack for term in query_terms):
            # Keep obviously relevant rich results even when the label does not echo the full query.
            if not any(token in haystack for token in ("comedy", "stand up", "special", "watch", "youtube")):
                continue

        item = {
            "ref_id": str(getattr(el, "ref_id", "") or "").strip(),
            "label": label[:200],
            "role": (getattr(el, "role", "") or "").lower(),
            "tag": (getattr(el, "tag", "") or "").lower(),
            "rank": len(items) + 1,
            "href": href,
            "href_domain": _url_domain(href),
        }
        if context:
            item["context"] = context[:140]
        if getattr(el, "action_types", None):
            item["actions"] = list(getattr(el, "action_types", []) or [])
        items.append(item)
        seen_hrefs.add(href)

    return items


_FORM_ROLES = frozenset({"textbox", "searchbox", "combobox", "listbox", "radio", "checkbox"})
_STRUCTURED_ITEM_ALIASES = {
    "link": "links",
    "links": "links",
    "url": "links",
    "urls": "links",
    "search result": "results",
    "search results": "results",
    "search_result": "results",
    "search_results": "results",
    "search result links": "results",
    "search_results_links": "results",
    "result": "results",
    "results": "results",
    "listing": "products",
    "listings": "products",
    "product": "products",
    "products": "products",
    "property listing": "products",
    "property listings": "products",
    "apartment listing": "products",
    "apartment listings": "products",
    "apartment rental listing": "products",
    "apartment rental listings": "products",
    "table row": "table_rows",
    "table rows": "table_rows",
    "table_row": "table_rows",
    "table_rows": "table_rows",
    "list item": "list_items",
    "list items": "list_items",
    "list_item": "list_items",
    "list_items": "list_items",
    "everything": "all",
    "any": "all",
    "all": "all",
}


def _normalize_structured_item_type(item_type: str) -> str:
    normalized = _norm(item_type).replace("-", " ").replace("_", " ").strip()
    if not normalized:
        return "all"
    if normalized in _STRUCTURED_ITEM_ALIASES:
        return _STRUCTURED_ITEM_ALIASES[normalized]
    if "search" in normalized and "result" in normalized:
        return "results"
    if "link" in normalized or "url" in normalized:
        return "links"
    if "table" in normalized or "row" in normalized:
        return "table_rows"
    if "list" in normalized:
        return "list_items"
    if any(term in normalized for term in ("product", "listing", "property", "apartment", "flat", "rental", "home")):
        return "products"
    return "all"


def _snapshot_stats(snapshot) -> dict[str, int]:
    links = 0
    forms = 0
    images = 0
    text_blocks = 0
    for el in snapshot.elements:
        if not getattr(el, "visible", True):
            continue
        role = (el.role or "").lower()
        tag = (el.tag or "").lower()
        label = (el.primary_label() or "").strip()
        if tag == "a" or role == "link" or getattr(el, "href", ""):
            links += 1
        if role in _FORM_ROLES or tag in ("input", "textarea", "select"):
            forms += 1
        if tag == "img":
            images += 1
        if tag in ("p", "article", "section", "div") and len(label) > 50:
            text_blocks += 1
    return {
        "link_count": links,
        "form_field_count": forms,
        "image_count": images,
        "text_block_count": text_blocks,
    }


def _element_by_ref(snapshot, ref_id: str):
    for el in snapshot.elements:
        if el.ref_id == ref_id:
            return el
    return None


def _coerce_confidence(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════
#  ACI Tool: find_and_act
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="find_and_act",
    description=(
        "Find a target element on the current browser page and perform an action "
        "on it (click, type, or select). Automatically scrolls the page to locate "
        "the element if it is not in the current viewport. This is the PREFERRED "
        "tool for all browser interactions — use it instead of manually calling "
        "browser_read_page + browser_click_ref.\n\n"
        "Examples:\n"
        "  find_and_act(target='Submit button', action='click')\n"
        "  find_and_act(target='Email field', action='type', value='user@mail.com')\n"
        "  find_and_act(target='Country dropdown', action='select', value='United Kingdom')\n"
        "  find_and_act(target='first search result', action='click')"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Human description of the element to find: 'Submit button', "
                    "'first search result link', 'Email input field', 'Price of the first item'"
                ),
            },
            "action": {
                "type": "string",
                "description": "What to do with the element",
                "enum": ["click", "type", "select"],
            },
            "value": {
                "type": "string",
                "description": "Text to type or option to select (required for type/select actions)",
            },
            "scroll_to_find": {
                "type": "boolean",
                "description": "If True (default), auto-scroll the page to locate the element",
            },
            "session_id": {
                "type": "string",
                "description": "Optional browser session id",
            },
        },
        "required": ["target", "action"],
    },
)
async def find_and_act(
    target: str,
    action: str = "click",
    value: str = "",
    scroll_to_find: bool = True,
    session_id: str = "",
) -> str:
    """Find an element on the page and act on it, with auto-scrolling."""

    # 1. Get current snapshot
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return _error_payload(error, target=target, action=action, error_code="no_snapshot")

    resolved_session = session_id or snapshot.session_id

    # 2. Try to find the element in the current viewport first
    selection, sel_error = await select_browser_candidate_with_flash(
        query=target,
        action=action,
        session_id=resolved_session,
        limit=5,
    )

    best_ref = ""
    selection_note = ""
    if not sel_error:
        best_ref = str(selection.get("ref_id") or "").strip()
        selection_note = str(selection.get("reason") or "").strip()
    scroll_count = 0

    # 3. If not found and scrolling is allowed, scroll and retry
    if not best_ref and scroll_to_find:
        for attempt in range(_MAX_SCROLL_ATTEMPTS):
            # Check if we're at the bottom
            vp = snapshot.viewport
            page_h = getattr(vp, "page_height", 0) or getattr(vp, "scroll_height", 0) or 0
            at_bottom = (vp.scroll_y + vp.height) >= (page_h - 5) if page_h > 0 else False
            if at_bottom:
                break

            # Scroll down
            await browser_scroll(direction="down", amount="page", session_id=resolved_session)
            scroll_count += 1
            await asyncio.sleep(_SCROLL_SETTLE_S)

            # Refresh snapshot and try again
            snapshot = _lookup_snapshot(resolved_session)
            if not snapshot:
                break

            selection, sel_error = await select_browser_candidate_with_flash(
                query=target,
                action=action,
                session_id=resolved_session,
                limit=5,
            )
            if not sel_error:
                best_ref = str(selection.get("ref_id") or "").strip()
                selection_note = str(selection.get("reason") or "").strip()
            else:
                best_ref = ""
            if best_ref:
                break

    # 4. If still not found, report failure
    if not best_ref:
        return _error_payload(
            f"Could not find element matching '{target}' on the page "
            f"(scrolled {scroll_count} times).",
            target=target,
            action=action,
            scrolled=scroll_count,
            selector_note=selection_note,
            error_code="element_not_found",
        )

    # 5. Perform the action
    element = _resolve_element(best_ref, resolved_session)
    if not element:
        return _error_payload(
            f"Element ref '{best_ref}' expired after scrolling.",
            target=target,
            action=action,
            ref_id=best_ref,
            error_code="stale_ref",
        )

    if action == "click":
        action_result = json.loads(
            await _queue_browser_action("click", best_ref, session_id=resolved_session)
        )
    elif action == "type":
        if not value:
            return _error_payload(
                "Action 'type' requires a 'value' argument.",
                target=target,
                action=action,
                error_code="missing_value",
            )
        action_result = json.loads(
            await _queue_browser_action(
                "type", best_ref, session_id=resolved_session, text=value, clear_first=True
            )
        )
    elif action == "select":
        if not value:
            return _error_payload(
                "Action 'select' requires a 'value' argument.",
                target=target,
                action=action,
                error_code="missing_value",
            )
        action_result = json.loads(
            await _queue_browser_action(
                "select", best_ref, session_id=resolved_session, option=value
            )
        )
    else:
        return _error_payload(
            f"Unknown action '{action}'. Use click, type, or select.",
            target=target,
            action=action,
            error_code="unknown_action",
        )

    selection_reason = selection.get("reason", "")

    return json.dumps(
        {
            "ok": action_result.get("ok", False),
            "target": target,
            "action": action,
            "ref_id": best_ref,
            "element_label": element.primary_label(),
            "element_role": element.role or element.tag,
            "scrolled": scroll_count,
            "selection_reason": selection_reason,
            "selection_model": selection.get("model", ""),
            "degraded_mode": bool(selection.get("degraded_mode")),
            "degraded_reason": selection.get("degraded_reason", ""),
            "action_result": action_result,
        },
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════════
#  ACI Tool: get_page_summary
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="get_page_summary",
    description=(
        "Get a Flash-backed summary of the current browser page: page type, "
        "key headings, important targets, and scroll position. Use this to "
        "understand what kind of page you're looking at before deciding how "
        "to interact with it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Optional browser session id",
            },
        },
        "required": [],
    },
)
async def get_page_summary(session_id: str = "") -> str:
    """Interpret the current page with Flash and return a structured summary."""
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return _error_payload(error, error_code="no_snapshot")

    # Show scanning visual on the page while AI analyzes
    page_domain = _url_domain(snapshot.url) or "page"
    _queue_scanning_action(
        snapshot,
        label=f"Analyzing {page_domain}…",
        duration_ms=6000,
        session_id=session_id,
    )

    try:
        interpreted = await summarize_page_with_flash(snapshot)
    except BrowserInterpretationError as exc:
        _queue_scanning_action(snapshot, start=False, session_id=session_id)
        return _error_payload(
            exc.reason,
            error_code=exc.error_code,
            url=snapshot.url,
            title=snapshot.title,
            degraded_mode=True,
            route="flash_browser_interpreter",
        )

    # Stop scanning — analysis is done
    _queue_scanning_action(snapshot, start=False, session_id=session_id)

    headings: list[dict[str, Any]] = []
    heading_agent_ids: list[int] = []
    for raw_heading in interpreted.get("headings", [])[:10]:
        if not isinstance(raw_heading, dict):
            continue
        ref_id = str(raw_heading.get("ref_id", "")).strip()
        text = str(raw_heading.get("text", "")).strip()
        el = _element_by_ref(snapshot, ref_id) if ref_id else None
        if not text and el:
            text = (el.primary_label() or "").strip()
        if not text:
            continue
        tag = str(raw_heading.get("tag", "")).strip() or (el.tag if el else "")
        headings.append(
            {
                "ref_id": ref_id,
                "text": text[:140],
                "tag": tag[:24] if tag else "",
            }
        )
        if el and getattr(el, "agent_id", 0):
            heading_agent_ids.append(el.agent_id)

    summary_text = str(interpreted.get("summary", "")).strip()
    page_type = str(interpreted.get("page_type", "")).strip() or "unknown"
    stats = _snapshot_stats(snapshot)

    vp = snapshot.viewport
    page_height = getattr(vp, "page_height", 0) or getattr(vp, "scroll_height", 0) or 0

    summary_lines = [f"Page type: {page_type}"]
    if summary_text:
        summary_lines.append(summary_text)
    summary_lines.extend(str(item.get("text", "")).strip() for item in headings[:5] if isinstance(item, dict))
    _queue_research_highlight(
        snapshot,
        session_id=session_id,
        tool_name="get_page_summary",
        mode="summary",
        duration_ms=3500,
        agent_ids=heading_agent_ids,
        source_url=snapshot.url,
        title=snapshot.title or "",
        snippet="\n".join(line for line in summary_lines if line),
        item_count=len(headings),
    )

    return json.dumps(
        {
            "url": snapshot.url,
            "title": snapshot.title or "",
            "page_type": page_type,
            "summary": summary_text,
            "headings": headings[:10],
            "key_targets": interpreted.get("key_targets", [])[:5] if isinstance(interpreted.get("key_targets"), list) else [],
            "link_count": stats["link_count"],
            "form_field_count": stats["form_field_count"],
            "image_count": stats["image_count"],
            "text_block_count": stats["text_block_count"],
            "total_elements": len(snapshot.elements),
            "scroll_y": vp.scroll_y,
            "page_height": page_height,
            "viewport_height": vp.height,
            "at_bottom": (vp.scroll_y + vp.height) >= (page_height - 5) if page_height > 0 else False,
            "generation": snapshot.generation,
            "confidence": _coerce_confidence(interpreted.get("confidence", 0.0)),
            "interpreter_model": interpreted.get("_interpreter_model", ""),
            "degraded_mode": False,
            "degraded_reason": "",
        },
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════════
#  ACI Tool: read_page_content
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="read_page_content",
    description=(
        "Read the readable text content from the current browser page for research "
        "and content extraction. Automatically scrolls to collect more content if "
        "the page is longer than the viewport. Returns clean paragraphs without "
        "navigation chrome. Use this for extracting article text, research data, "
        "or any substantive page content.\n\n"
        "Unlike browser_read_page (which returns ref IDs for interaction), this "
        "returns clean text optimized for reading and research."
    ),
    parameters={
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to collect (default 8000)",
            },
            "scroll_pages": {
                "type": "integer",
                "description": "Max pages to scroll for more content (default 3, 0=viewport only)",
            },
            "query": {
                "type": "string",
                "description": "Optional filter — only include paragraphs containing this term",
            },
            "session_id": {
                "type": "string",
                "description": "Optional browser session id",
            },
        },
        "required": [],
    },
)
async def read_page_content(
    max_chars: int = 8000,
    scroll_pages: int = 3,
    query: str = "",
    session_id: str = "",
) -> str:
    """Extract readable content from the page, scrolling as needed."""

    # Show scanning visual while reading
    snap_for_scan = _lookup_snapshot(session_id)
    if snap_for_scan:
        page_domain = _url_domain(getattr(snap_for_scan, "url", "") or "") or "page"
        _queue_scanning_action(
            snap_for_scan,
            label=f"Reading {page_domain}…",
            duration_ms=8000,
            session_id=session_id,
        )

    # Read initial viewport
    result_json = await browser_read_text(
        max_chars=max_chars, query=query, session_id=session_id, refresh=True
    )
    try:
        result = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return result_json

    all_content = result.get("content", "")
    total_chars = len(all_content)

    # Scroll and collect more if needed
    if scroll_pages > 0 and total_chars < max_chars and not result.get("at_bottom", True):
        for _ in range(scroll_pages):
            if total_chars >= max_chars:
                break
            # Scroll down
            await browser_scroll(direction="down", amount="page", session_id=session_id)
            await asyncio.sleep(_SCROLL_SETTLE_S)

            # Read the new viewport content
            more_json = await browser_read_text(
                max_chars=max_chars - total_chars,
                query=query,
                session_id=session_id,
                refresh=True,
            )
            try:
                more = json.loads(more_json)
            except (json.JSONDecodeError, TypeError):
                break

            new_content = more.get("content", "")
            if not new_content or new_content == all_content[-len(new_content):]:
                # No new content or duplicate — stop scrolling
                break

            all_content += "\n\n" + new_content
            total_chars = len(all_content)

            if more.get("at_bottom", False):
                break

    # Rebuild final result
    result["content"] = all_content[:max_chars]
    result["content_length"] = len(result["content"])
    result["scrolled_pages"] = scroll_pages

    if result["content_length"] == 0:
        _queue_scanning_action(_lookup_snapshot(session_id), start=False, session_id=session_id)
        return _error_payload(
            "No readable content extracted from the current page.",
            error_code="empty_content",
            url=result.get("url", ""),
            title=result.get("title", ""),
            content_length=0,
            paragraph_count=result.get("paragraph_count", 0),
            scroll_y=result.get("scroll_y", 0),
        )

    _queue_research_highlight(
        _lookup_snapshot(session_id),
        session_id=session_id,
        tool_name="read_page_content",
        mode="text",
        duration_ms=4500,
        source_url=result.get("url", ""),
        title=result.get("title", ""),
        snippet=result["content"],
        item_count=result.get("paragraph_count", 0),
    )

    # Stop scanning now that content is collected
    _queue_scanning_action(_lookup_snapshot(session_id), start=False, session_id=session_id)

    return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  ACI Tool: extract_structured_data
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="extract_structured_data",
    description=(
        "Extract structured items from the current browser page — search result "
        "links, product listings, table rows, or any repeated pattern. Returns "
        "a list of items with their labels, ref IDs, and any associated metadata.\n\n"
        "Use this to extract lists of results, products, links, or any structured "
        "data visible on the page."
    ),
    parameters={
        "type": "object",
        "properties": {
            "item_type": {
                "type": "string",
                "description": "Type of items to extract",
                "enum": ["links", "results", "products", "table_rows", "list_items", "all"],
            },
            "query": {
                "type": "string",
                "description": "Optional filter to narrow results (e.g. 'rental', 'price')",
            },
            "max_items": {
                "type": "integer",
                "description": "Maximum items to return (default 20)",
            },
            "session_id": {
                "type": "string",
                "description": "Optional browser session id",
            },
        },
        "required": ["item_type"],
    },
)
async def extract_structured_data(
    item_type: str = "all",
    query: str = "",
    max_items: int = 20,
    session_id: str = "",
) -> str:
    """Extract structured items from the current page."""

    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return _error_payload(error, error_code="no_snapshot")

    # Show scanning visual while extracting
    page_domain = _url_domain(snapshot.url) or "page"
    _queue_scanning_action(
        snapshot,
        label=f"Extracting data from {page_domain}…",
        duration_ms=6000,
        session_id=session_id,
    )

    normalized_item_type = _normalize_structured_item_type(item_type)
    try:
        interpreted = await extract_structured_items_with_flash(
            snapshot,
            item_type=normalized_item_type,
            query=query,
            max_items=max_items,
        )
    except BrowserInterpretationError as exc:
        fallback_items = []
        fallback_strategy = ""
        # Try deterministic search-result extraction
        if normalized_item_type in {"results", "links", "all"}:
            fallback_items = _deterministic_search_items(
                snapshot,
                query=query,
                max_items=max_items,
            )
            fallback_strategy = "deterministic-search-resolver"
        # Try shopping/product card extraction
        if not fallback_items and normalized_item_type in {"products", "shopping", "results", "all"}:
            shop_items, shop_meta = extract_shopping_items(
                snapshot, query=query, max_items=max_items,
            )
            if shop_items:
                fallback_items = shop_items
                fallback_strategy = "deterministic-product-cards"
        if fallback_items:
            vp = snapshot.viewport
            page_height = getattr(vp, "page_height", 0) or getattr(vp, "scroll_height", 0) or 0
            _queue_scanning_action(snapshot, start=False, session_id=session_id)
            return json.dumps(
                {
                    "url": snapshot.url,
                    "title": snapshot.title,
                    "page_type": "search_results",
                    "item_type": normalized_item_type,
                    "requested_item_type": item_type,
                    "query": query,
                    "items": fallback_items,
                    "item_count": len(fallback_items),
                    "total_elements": len(snapshot.elements),
                    "scroll_y": vp.scroll_y,
                    "page_height": page_height,
                    "at_bottom": (vp.scroll_y + vp.height) >= (page_height - 5) if page_height > 0 else False,
                    "generation": snapshot.generation,
                    "notes": f"Used {fallback_strategy} after Flash extraction failed.",
                    "confidence": 0.55,
                    "interpreter_model": fallback_strategy,
                    "degraded_mode": True,
                    "degraded_reason": exc.reason,
                },
                ensure_ascii=False,
            )
        _queue_scanning_action(snapshot, start=False, session_id=session_id)
        return _error_payload(
            exc.reason,
            error_code=exc.error_code,
            item_type=normalized_item_type,
            requested_item_type=item_type,
            query=query,
            item_count=0,
            total_elements=len(snapshot.elements),
            url=snapshot.url,
            title=snapshot.title,
            degraded_mode=True,
            route="flash_browser_interpreter",
        )

    items: list[dict[str, Any]] = []
    highlight_agent_ids: list[int] = []
    seen_refs: set[str] = set()
    for raw_item in interpreted.get("items", [])[: max(1, min(int(max_items or 20), 20))]:
        if not isinstance(raw_item, dict):
            continue
        ref_id = str(raw_item.get("ref_id", "")).strip()
        if not ref_id or ref_id in seen_refs:
            continue
        el = _element_by_ref(snapshot, ref_id)
        if not el:
            continue
        label = str(raw_item.get("label", "")).strip() or (el.primary_label() or "").strip()
        if not label:
            continue
        item = {
            "ref_id": ref_id,
            "label": label[:200],
            "role": (el.role or "").lower(),
            "tag": (el.tag or "").lower(),
            "rank": len(items) + 1,
        }
        href = str(raw_item.get("href", "")).strip() or (el.href or "").strip()
        if href:
            item["href"] = href
            item["href_domain"] = _url_domain(href)
        context = str(raw_item.get("context", "")).strip() or (el.context_text or "").strip()
        if context:
            item["context"] = context[:120]
        reason = str(raw_item.get("reason", "")).strip()
        if reason:
            item["reason"] = reason[:200]
        if el.action_types:
            item["actions"] = el.action_types
        items.append(item)
        seen_refs.add(ref_id)
        if getattr(el, "agent_id", 0):
            highlight_agent_ids.append(el.agent_id)

    vp = snapshot.viewport
    page_height = getattr(vp, "page_height", 0) or getattr(vp, "scroll_height", 0) or 0

    if not items:
        _queue_scanning_action(snapshot, start=False, session_id=session_id)
        return _error_payload(
            "No structured items matched on the current page.",
            error_code="empty_items",
            item_type=normalized_item_type,
            requested_item_type=item_type,
            query=query,
            item_count=0,
            total_elements=len(snapshot.elements),
            url=snapshot.url,
            title=snapshot.title,
        )

    preview_rows = []
    for item in items[:5]:
        label = str(item.get("label", "")).strip()
        context = str(item.get("context", "")).strip()
        href = str(item.get("href", "")).strip()
        row = " | ".join(part for part in (label, context, href) if part)
        if row:
            preview_rows.append(row)

    _queue_research_highlight(
        snapshot,
        session_id=session_id,
        tool_name="extract_structured_data",
        mode="results",
        duration_ms=4000,
        agent_ids=highlight_agent_ids,
        source_url=snapshot.url,
        title=snapshot.title,
        snippet="\n".join(preview_rows),
        item_count=len(items),
    )

    # Stop scanning — extraction is done
    _queue_scanning_action(snapshot, start=False, session_id=session_id)

    return json.dumps(
        {
            "url": snapshot.url,
            "title": snapshot.title,
            "page_type": str(interpreted.get("page_type", "")).strip() or "page",
            "item_type": normalized_item_type,
            "requested_item_type": item_type,
            "query": query,
            "items": items,
            "item_count": len(items),
            "total_elements": len(snapshot.elements),
            "scroll_y": vp.scroll_y,
            "page_height": page_height,
            "at_bottom": (vp.scroll_y + vp.height) >= (page_height - 5) if page_height > 0 else False,
            "generation": snapshot.generation,
            "notes": str(interpreted.get("notes", "")).strip(),
            "confidence": _coerce_confidence(interpreted.get("confidence", 0.0)),
            "interpreter_model": interpreted.get("_interpreter_model", ""),
            "degraded_mode": False,
            "degraded_reason": "",
        },
        ensure_ascii=False,
    )
