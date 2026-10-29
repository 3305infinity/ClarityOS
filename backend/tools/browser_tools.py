"""
ClarityOS — Browser Ref Tools
============================
Agent-facing tools for stable, verified browser element access.
"""

import asyncio
import json
import time
from typing import Any, Optional

from browser.bridge import browser_bridge, bridge_disconnect_reason, read_page_via_owner
from browser.models import ActionRequest
from browser.selector_ai import select_browser_candidate_with_flash
from browser.store import browser_store
from browser.verifier import verify_action_result
from runtime_state import runtime_state_store
from tools.contracts import dumps as contract_dumps
from tools.contracts import error_envelope
from tools.registry import registry


def _error_payload(message: str, **context) -> str:
    error_code = str(context.pop("error_code", "") or "browser.unknown").strip()
    session_id = str(context.get("session_id", "") or "").strip()
    payload = error_envelope(
        error_code,
        message,
        session_id=session_id,
        source="tool.browser",
        details=context,
        flatten_details=True,
    )
    return contract_dumps(payload)


def _lookup_snapshot(session_id: str = ""):
    snapshot = browser_store.get_snapshot(session_id or None)
    if snapshot:
        return snapshot
    
    # If session_id is missing or not found, try to return the most recent snapshot
    # across ALL sessions. This handles background tab activity better than a hard None.
    global_snap = browser_store.get_snapshot(None)
    if global_snap:
        return global_snap
        
    return None


async def _require_snapshot(session_id: str = "", timeout: float = 1.5):
    snapshot = _lookup_snapshot(session_id)
    if snapshot:
        return snapshot, ""

    # Use event-driven wait instead of polling
    snapshot_obj = await browser_bridge.wait_for_snapshot(
        session_id=session_id, min_generation=0, timeout=max(0.05, timeout)
    )
    if snapshot_obj:
        return snapshot_obj, ""

    return None, (
        "ERROR: No active browser snapshot is available. The Chrome extension bridge "
        "must connect and publish a page snapshot before browser ref tools can be used."
    )


def _resolve_element(ref_id: str, session_id: str = ""):
    element = browser_store.get_element(ref_id, session_id or None)
    if element:
        return element
    if session_id:
        return browser_store.get_element(ref_id, None)
    return None


def _snapshot_health(snapshot) -> dict:
    age_seconds = max(0.0, time.time() - float(snapshot.timestamp or 0.0))
    return {
        "age_seconds": round(age_seconds, 2),
        "stale": age_seconds > 5.0,
    }


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _clean_research_snippet(text: str, max_chars: int = 420) -> str:
    cleaned = " ".join((text or "").strip().split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def _strip_readable_prefix(line: str) -> str:
    cleaned = (line or "").strip()
    if cleaned.startswith("[") and "]" in cleaned:
        cleaned = cleaned.split("]", 1)[1].strip()
    if cleaned.startswith("(") and ")" in cleaned:
        cleaned = cleaned.split(")", 1)[1].strip()
    return cleaned


def _build_research_highlight_metadata(
    snapshot,
    *,
    tool_name: str = "",
    mode: str = "text",
    duration_ms: int = 4000,
    agent_ids: Optional[list[int]] = None,
    source_url: str = "",
    title: str = "",
    snippet: str = "",
    item_count: int = 0,
) -> dict:
    metadata = {
        "tab_id": getattr(snapshot, "tab_id", "") if snapshot else "",
        "duration": str(max(0, int(duration_ms or 0))),
        "mode": mode or "text",
        "tool": tool_name or "",
        "title": title or (getattr(snapshot, "title", "") if snapshot else ""),
        "source_url": source_url or (getattr(snapshot, "url", "") if snapshot else ""),
        "snippet": _clean_research_snippet(snippet),
        "item_count": str(max(0, int(item_count or 0))),
    }
    if agent_ids:
        metadata["agent_ids"] = [int(agent_id) for agent_id in agent_ids[:30] if agent_id]
    return metadata


def _queue_research_highlight(
    snapshot,
    *,
    session_id: str = "",
    tool_name: str = "",
    mode: str = "text",
    duration_ms: int = 4000,
    agent_ids: Optional[list[int]] = None,
    source_url: str = "",
    title: str = "",
    snippet: str = "",
    item_count: int = 0,
) -> None:
    if not snapshot:
        return
    try:
        highlight_request = ActionRequest(
            action="highlight",
            ref_id="",
            session_id=session_id or getattr(snapshot, "session_id", ""),
            metadata=_build_research_highlight_metadata(
                snapshot,
                tool_name=tool_name,
                mode=mode,
                duration_ms=duration_ms,
                agent_ids=agent_ids,
                source_url=source_url,
                title=title,
                snippet=snippet,
                item_count=item_count,
            ),
        )
        browser_bridge.queue_action(highlight_request)
    except Exception:
        pass


def _page_content_is_usable(payload: dict) -> Optional[dict]:
    """
    Is this really the page the user is looking at?

    Returns None when the payload looks like genuine page content, or a failure
    dict explaining why it does not. Deliberately general — it tests the shape
    of the evidence, not any particular title:

      * a real page is served over http(s); browser-internal surfaces
        (chrome://, edge://, about:, devtools://) are the browser's own UI, not
        the user's page. A profile chooser or settings screen lands here.
      * a real page has some readable text. Near-empty extractions mean the
        reader ran before the document was ready, or on a blank surface.

    This exists because a summary was once produced from Chrome's profile
    chooser after the agent launched the browser mid-task: the retrieval
    "succeeded" and nothing checked what had actually been retrieved.
    """
    url = str(payload.get("url") or "").strip()
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or payload.get("content") or "")

    internal_schemes = ("chrome://", "edge://", "about:", "devtools://",
                        "chrome-extension://", "view-source:", "brave://")
    lowered = url.lower()

    if lowered and any(lowered.startswith(scheme) for scheme in internal_schemes):
        return {
            "ok": False,
            "error": "browser_internal_page",
            "message": (f"The active tab is a browser-internal page ({url}), not a web page "
                        f"I can read. Switch to the tab you want summarised and ask again."),
        }

    # A missing URL is not fatal. The cross-process owner payload does not carry
    # one, yet it returns real page text; rejecting it would break the very path
    # that works. Content length below still guards against empty surfaces.

    if len(text.strip()) < 80:
        return {
            "ok": False,
            "error": "insufficient_content",
            "message": (f"Only {len(text.strip())} characters of readable text came back from "
                        f"{title or url}. The page may still be loading or may not expose "
                        f"readable content."),
        }

    return None


async def _bridge_extract_readability(session_id: str = "", timeout: float = 4.0) -> dict[str, Any]:
    if not browser_bridge.is_connected():
        # The extension is attached to the other ClarityOS process (only one can
        # own port 8765). Borrow its connection over the hub's authenticated API
        # instead of failing — the user should not have to close a window.
        from browser.remote import remote_page
        borrowed = await remote_page(session_id=session_id, timeout=max(timeout, 10.0))
        if borrowed is not None and borrowed.get("ok"):
            borrowed = dict(borrowed)
            borrowed["via"] = "hub_bridge_owner"
            # Borrowed content gets the same scrutiny as local content: it was
            # skipping validation entirely, so a browser-internal surface read
            # through the owner would have been summarised as if it were a page.
            verdict = _page_content_is_usable(borrowed)
            if verdict is not None:
                return verdict
            return borrowed
        return {
            "ok": False,
            "message": (borrowed or {}).get("message") or bridge_disconnect_reason(),
            "error": (borrowed or {}).get("error") or "bridge_disconnected",
        }

    snapshot = _lookup_snapshot(session_id)
    resolved_session_id = session_id or (snapshot.session_id if snapshot else "") or (browser_bridge.connected_session_id() or "")
    if not resolved_session_id:
        return {
            "ok": False,
            "message": "No browser session is available for Readability extraction.",
            "error": "missing_session",
        }

    request = ActionRequest(
        action="extract_readability",
        ref_id="",
        session_id=resolved_session_id,
        timeout=timeout,
    )
    queued = browser_bridge.queue_action(request)
    if not queued.ok:
        return {
            "ok": False,
            "message": queued.message,
            "error": "queue_failed",
        }

    started = time.time()
    while time.time() - started < timeout:
        result = browser_bridge.latest_action_result(queued.action_id)
        if result:
            if not result.ok:
                return {
                    "ok": False,
                    "message": result.message,
                    "error": result.details.get("error", "action_failed"),
                }
            raw_payload = result.details.get("result", "")
            if not raw_payload:
                return {
                    "ok": False,
                    "message": "Readability extraction returned no payload.",
                    "error": "empty_payload",
                }
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "message": "Readability extraction returned invalid JSON.",
                    "error": "invalid_payload",
                }
            if not isinstance(payload, dict):
                return {
                    "ok": False,
                    "message": "Readability extraction returned an unexpected payload.",
                    "error": "invalid_payload",
                }
            if "content_length" in payload:
                try:
                    payload["content_length"] = int(payload.get("content_length", 0) or 0)
                except Exception:
                    payload["content_length"] = 0
            verdict = _page_content_is_usable(payload)
            if verdict is not None:
                return verdict
            runtime_state_store.record_readability_extraction(payload)
            return payload
        await asyncio.sleep(0.1)

    return {
        "ok": False,
        "message": "Timed out waiting for Readability extraction.",
        "error": "timeout",
    }


@registry.register(
    name="browser_snapshot",
    description="Return the current structured browser snapshot summary from the extension bridge: URL, title, generation, visible interactive element count, and opaque regions.",
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": []
    }
)
async def browser_snapshot(session_id: str = "") -> str:
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return error

    vp = snapshot.viewport
    page_height = getattr(vp, 'page_height', 0) or getattr(vp, 'scroll_height', 0) or 0
    summary = {
        "session_id": snapshot.session_id,
        "tab_id": snapshot.tab_id,
        "url": snapshot.url,
        "title": snapshot.title,
        "generation": snapshot.generation,
        "interactive_elements": len(snapshot.elements),
        "opaque_regions": snapshot.opaque_regions,
        "scroll_y": vp.scroll_y,
        "page_height": page_height,
        "viewport_height": vp.height,
        "at_bottom": (vp.scroll_y + vp.height) >= (page_height - 5) if page_height > 0 else False,
        **_snapshot_health(snapshot),
    }
    return json.dumps(summary, ensure_ascii=False)


@registry.register(
    name="browser_read_page",
    description=(
        "Read visible text content from the current browser page. Returns structured "
        "content with element ref IDs for further interaction, plus scroll position "
        "(scroll_y, page_height, at_bottom). Use this FIRST to understand what is on "
        "a page — product names, prices, search results, article text — before deciding "
        "what to click or interact with. Set refresh=true after scrolling to get fresh content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "max_items": {"type": "integer", "description": "Max elements to return (default 60)"},
            "viewport_only": {"type": "boolean", "description": "Only return elements visible in the viewport (default true)"},
            "query": {"type": "string", "description": "Optional filter — only return elements whose text contains this substring"},
            "session_id": {"type": "string", "description": "Optional browser session id"},
            "refresh": {"type": "boolean", "description": "Force a fresh snapshot from the browser before reading (default false). Use after scrolling or when content may have changed."},
        },
        "required": []
    }
)
async def browser_read_page(max_items: int = 60, viewport_only: bool = True, query: str = "", session_id: str = "", refresh: bool = False) -> str:
    # Force a fresh snapshot if requested
    if refresh:
        await browser_refresh_refs(session_id=session_id, timeout=1.5)
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return error

    query_norm = _norm(query)
    content_lines: list[str] = []
    reading_agent_ids: list[int] = []  # Track which elements we're reading for highlighting
    count = 0

    for element in snapshot.elements:
        if count >= max_items:
            break
        if not element.visible:
            continue
        if viewport_only and not getattr(element, "in_viewport", True):
            continue

        label = (element.primary_label() or "").strip()
        if not label or len(label) < 2:
            continue

        # Optional substring filter
        if query_norm and query_norm not in _norm(label) and query_norm not in _norm(element.context_text):
            continue

        role = element.role or element.tag
        is_interactive = bool(
            element.action_types
            and any(a in element.action_types for a in ("click", "type", "fill", "select"))
        )

        # The extension already captures each anchor's href and the store keeps
        # it, but this formatter dropped it — so the only way to answer "what is
        # the link in this email" was from the model's own memory, which is how
        # an unrelated URL came back as if it had been read off the page.
        # Showing the real destination is what makes a grounded answer possible.
        href = str(getattr(element, "href", "") or "").strip()
        if href and not href.lower().startswith(("javascript:", "#")):
            try:
                from browser.listing_extractor import _normalize_href
                href = _normalize_href(str(snapshot.url or ""), href)
            except Exception:
                pass
            suffix = f" -> {href[:300]}"
        else:
            suffix = ""

        if is_interactive:
            content_lines.append(f"[{element.ref_id}] ({role}) {label}{suffix}")
        else:
            content_lines.append(f"  ({role}) {label}{suffix}")

        # Collect agent IDs for highlighting
        if hasattr(element, 'agent_id') and element.agent_id:
            reading_agent_ids.append(element.agent_id)

        count += 1

    if reading_agent_ids:
        snippet_preview = " | ".join(
            _strip_readable_prefix(line)
            for line in content_lines[:6]
            if _strip_readable_prefix(line)
        )
        _queue_research_highlight(
            snapshot,
            session_id=session_id,
            tool_name="browser_read_page",
            mode="text",
            duration_ms=4000,
            agent_ids=reading_agent_ids,
            source_url=snapshot.url,
            title=snapshot.title,
            snippet=snippet_preview,
            item_count=count,
        )

    vp = snapshot.viewport
    page_height = getattr(vp, 'page_height', 0) or getattr(vp, 'scroll_height', 0) or 0
    scroll_y = vp.scroll_y
    at_bottom = (scroll_y + vp.height) >= (page_height - 5) if page_height > 0 else False

    return json.dumps({
        "url": snapshot.url,
        "title": snapshot.title,
        "generation": snapshot.generation,
        **_snapshot_health(snapshot),
        "viewport_only": viewport_only,
        "element_count": count,
        "total_elements": len(snapshot.elements),
        "scroll_y": scroll_y,
        "page_height": page_height,
        "viewport_height": vp.height,
        "at_bottom": at_bottom,
        "content": "\n".join(content_lines),
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  browser_read_text — Extract readable article/page text
# ═══════════════════════════════════════════════════════════════

# Roles / tags that carry readable text content (not chrome / controls)
_TEXT_ROLES = frozenset({
    "paragraph", "heading", "text", "article", "blockquote", "listitem",
    "caption", "definition", "note", "contentinfo", "main", "region",
    "figure", "figcaption", "term", "time", "mark", "strong", "emphasis",
    "code", "pre", "cell", "gridcell",
})
_TEXT_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "figcaption", "caption", "td", "th", "pre", "code", "span", "em",
    "strong", "b", "i", "mark", "time", "summary", "dt", "dd", "article",
    "section", "main", "div",
})
# Tags that are definitely NOT content
_CHROME_TAGS = frozenset({
    "nav", "header", "footer", "aside", "menu", "menubar", "toolbar",
    "tablist", "tab", "dialog", "alertdialog", "banner", "navigation",
    "complementary", "form", "search",
})
_CHROME_LABEL_HINTS = frozenset({
    "skip to", "accessibility", "sign in", "sign up", "log in", "cookie",
    "accept all", "reject all", "privacy", "terms of", "menu", "navigation",
    "subscribe", "newsletter",
})


@registry.register(
    name="browser_read_text",
    description=(
        "Extract readable article text from the current browser page — paragraphs, "
        "headings, lists, and other content elements. Unlike browser_read_page (which "
        "returns interactive element ref IDs for clicking), this returns clean readable "
        "text suitable for research and content extraction. Use this AFTER navigating "
        "to an article or source page to extract its content for research."
    ),
    parameters={
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters of text to return (default 6000)"
            },
            "query": {
                "type": "string",
                "description": "Optional filter — only return paragraphs containing this term"
            },
            "session_id": {
                "type": "string",
                "description": "Optional browser session id"
            },
            "refresh": {
                "type": "boolean",
                "description": "Force a fresh snapshot before reading (default false)"
            },
        },
        "required": []
    }
)
async def browser_read_text(max_chars: int = 6000, query: str = "", session_id: str = "", refresh: bool = False) -> str:
    """Extract clean readable text from the current page for research / content extraction."""
    if refresh:
        await browser_refresh_refs(session_id=session_id, timeout=1.5)
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return error

    query_norm = _norm(query)
    paragraphs: list[str] = []
    seen_text: set[str] = set()  # De-duplicate repeated text fragments
    total_chars = 0
    reading_agent_ids: list[int] = []

    for element in snapshot.elements:
        if total_chars >= max_chars:
            break
        if not element.visible:
            continue

        role = (element.role or "").lower().strip()
        tag = (element.tag or "").lower().strip()
        label = (element.primary_label() or "").strip()

        # Skip elements with very short text (nav items, buttons, icons)
        if len(label) < 15:
            continue

        # Skip chrome / boilerplate
        if role in _CHROME_TAGS or tag in _CHROME_TAGS:
            continue
        label_lower = label.lower()
        if any(hint in label_lower for hint in _CHROME_LABEL_HINTS):
            continue

        # Must be a text-bearing element
        is_text_element = (
            role in _TEXT_ROLES
            or tag in _TEXT_TAGS
            or (not element.action_types and len(label) > 40)  # Long non-interactive = likely text
        )
        if not is_text_element:
            continue

        # Optional query filter
        if query_norm and query_norm not in _norm(label):
            continue

        # De-duplicate: skip if we've already seen very similar text
        text_key = label[:80].lower()
        if text_key in seen_text:
            continue
        seen_text.add(text_key)

        # Format based on role
        if role == "heading" or tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            heading_level = tag[1] if tag.startswith("h") and len(tag) == 2 else "2"
            prefix = "#" * int(heading_level) + " " if heading_level.isdigit() else "## "
            paragraphs.append(prefix + label)
        elif role == "listitem" or tag in ("li", "dt", "dd"):
            paragraphs.append("• " + label)
        else:
            paragraphs.append(label)

        total_chars += len(label)

        # Collect agent IDs for highlighting
        if hasattr(element, 'agent_id') and element.agent_id:
            reading_agent_ids.append(element.agent_id)

    content = "\n\n".join(paragraphs) if paragraphs else ""

    if reading_agent_ids or content:
        _queue_research_highlight(
            snapshot,
            session_id=session_id,
            tool_name="browser_read_text",
            mode="text",
            duration_ms=4000,
            agent_ids=reading_agent_ids,
            source_url=snapshot.url,
            title=snapshot.title,
            snippet=content,
            item_count=len(paragraphs),
        )

    vp = snapshot.viewport
    page_height = getattr(vp, 'page_height', 0) or getattr(vp, 'scroll_height', 0) or 0
    scroll_y = vp.scroll_y
    at_bottom = (scroll_y + vp.height) >= (page_height - 5) if page_height > 0 else False

    return json.dumps({
        "url": snapshot.url,
        "title": snapshot.title,
        "paragraph_count": len(paragraphs),
        "content_length": total_chars,
        "scroll_y": scroll_y,
        "page_height": page_height,
        "at_bottom": at_bottom,
        "content": content,
    }, ensure_ascii=False)


@registry.register(
    name="browser_find",
    description="Find ranked browser element candidates for a user intent. Returns ranked candidates and a best-candidate recommendation chosen by Gemini 3 Flash when available. Prefer browser_read_page to see page content first, and browser_click_match for quick clicks.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Human description like 'Continue button under billing' or 'email field'"},
            "action": {"type": "string", "description": "Desired action", "enum": ["click", "type", "fill", "select"]},
            "limit": {"type": "integer", "description": "Max candidates to return (default 5)"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["query"]
    }
)
async def browser_find(query: str, action: str = "click", limit: int = 5, session_id: str = "") -> str:
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return error

    selection, _ = await select_browser_candidate_with_flash(
        query=query,
        action=action,
        session_id=snapshot.session_id,
        limit=max(1, min(limit, 10)),
    )
    candidates = selection.get("candidates", [])
    best_ref_id = selection.get("ref_id", "")
    best_candidate = next((candidate for candidate in candidates if candidate.get("ref_id") == best_ref_id), None)
    return json.dumps({
        "query": query,
        "action": action,
        "generation": snapshot.generation,
        **_snapshot_health(snapshot),
        "candidates": candidates,
        "best_candidate": best_candidate,
        "selection_reason": selection.get("reason", ""),
        "selection_model": selection.get("model", "deterministic-resolver"),
        "degraded_mode": bool(selection.get("degraded_mode")),
        "degraded_reason": selection.get("degraded_reason", ""),
    }, ensure_ascii=False)


@registry.register(
    name="browser_click_match",
    description="Resolve the best browser element for a click intent and queue the click in one tool call. Prefer this for simple browser actions like 'click the first result' or 'open one of the videos'.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Human description like 'video thumbnail or title' or 'Continue button'"},
            "limit": {"type": "integer", "description": "Max candidates to consider (default 5)"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["query"]
    }
)
async def browser_click_match(query: str, limit: int = 5, session_id: str = "") -> str:
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return _error_payload(
            error,
            query=query,
            action="click",
            session_id=session_id or "",
            error_code="no_snapshot",
        )

    selection, selection_error = await select_browser_candidate_with_flash(
        query=query,
        action="click",
        session_id=snapshot.session_id,
        limit=max(1, min(limit, 10)),
    )
    if selection_error:
        return _error_payload(
            selection_error,
            query=query,
            action="click",
            session_id=snapshot.session_id,
            error_code="selection_error",
        )

    best_ref_id = selection.get("ref_id", "")
    if not best_ref_id:
        return _error_payload(
            f"No browser candidate matched query '{query}' for click action.",
            query=query,
            action="click",
            session_id=snapshot.session_id,
            error_code="no_match",
        )

    best_element = _resolve_element(best_ref_id, snapshot.session_id)
    if not best_element:
        return _error_payload(
            f"Selected browser ref '{best_ref_id}' is no longer available.",
            query=query,
            action="click",
            ref_id=best_ref_id,
            session_id=snapshot.session_id,
            error_code="stale_ref",
        )
    
    action_payload = json.loads(await _queue_browser_action("click", best_ref_id, session_id=snapshot.session_id))

    return json.dumps({
        "query": query,
        "selection_model": selection.get("model", "deterministic-resolver"),
        "selection_reason": selection.get("reason", ""),
        "degraded_mode": bool(selection.get("degraded_mode")),
        "degraded_reason": selection.get("degraded_reason", ""),
        "selected_ref_id": best_ref_id,
        "selected_candidate": {
            "ref_id": best_element.ref_id,
            "label": best_element.primary_label(),
            "role": best_element.role or best_element.tag,
        },
        "candidates": selection.get("candidates", []),
        "action": action_payload,
        "generation": snapshot.generation,
        **_snapshot_health(snapshot),
    }, ensure_ascii=False)


@registry.register(
    name="browser_describe_ref",
    description="Describe a specific stable browser element ref in detail: label, role, actions, state, context, and generation.",
    parameters={
        "type": "object",
        "properties": {
            "ref_id": {"type": "string", "description": "Stable browser ref id"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["ref_id"]
    }
)
async def browser_describe_ref(ref_id: str, session_id: str = "") -> str:
    element = _resolve_element(ref_id, session_id)
    if not element:
        return _error_payload(
            f"Unknown browser ref '{ref_id}'.",
            ref_id=ref_id,
            session_id=session_id or "",
            error_code="unknown_ref",
        )

    payload = {
        "ref_id": element.ref_id,
        "generation": element.generation,
        "role": element.role,
        "tag": element.tag,
        "label": element.primary_label(),
        "text": element.text,
        "aria_label": element.aria_label,
        "name": element.name,
        "placeholder": element.placeholder,
        "context": element.context_text,
        "actions": element.action_types,
        "visible": element.visible,
        "enabled": element.enabled,
        "bounds": element.bounds,
    }
    return json.dumps(payload, ensure_ascii=False)


async def _queue_browser_action(action: str, ref_id: str, session_id: str = "", text: str = "", option: str = "", clear_first: bool = False, timeout: float = 5.0) -> str:
    element = _resolve_element(ref_id, session_id)
    if not element:
        return _error_payload(
            f"Unknown browser ref '{ref_id}'.",
            action=action,
            ref_id=ref_id,
            session_id=session_id or "",
            error_code="unknown_ref",
        )

    resolved_snapshot = _lookup_snapshot(session_id)
    resolved_session_id = session_id or (resolved_snapshot.session_id if resolved_snapshot else "")

    request = ActionRequest(
        action=action,
        ref_id=ref_id,
        session_id=resolved_session_id,
        text=text,
        option=option,
        clear_first=clear_first,
        timeout=timeout,
    )
    result = browser_bridge.queue_action(request)
    if not result.ok:
        report = verify_action_result(result)
        return json.dumps({
            "ok": False,
            "action": result.action,
            "ref_id": result.ref_id,
            "action_id": result.action_id,
            "message": result.message,
            "pre_generation": result.pre_generation,
            "post_generation": result.post_generation,
            "verification": {
                "success": report.success,
                "confidence": report.confidence,
                "checks_passed": report.checks_passed,
                "needs_replan": report.needs_replan,
            }
        }, ensure_ascii=False)

    # Wait for the action result and fresh snapshot
    action_timeout_s = timeout
    action_result = await browser_bridge.wait_for_result(result.action_id, timeout=action_timeout_s)
        
    if not action_result:
        # Timeout waiting for action execution
        report = verify_action_result(result)
        return json.dumps({
            "ok": True, # Queue was ok, but execution timed out
            "action": result.action,
            "ref_id": result.ref_id,
            "action_id": result.action_id,
            "message": "Action queued but execution timed out. Snapshot may still be updating.",
            "pre_generation": result.pre_generation,
            "post_generation": result.post_generation,
            "verification": {
                "success": report.success,
                "confidence": report.confidence,
                "checks_passed": report.checks_passed,
                "needs_replan": report.needs_replan,
            }
        }, ensure_ascii=False)

    # Wait briefly for a fresh snapshot after successful action
    if action_result.ok:
        pre_gen = action_result.pre_generation or 0
        fresh_snapshot = await browser_bridge.wait_for_snapshot(
            session_id=resolved_session_id, min_generation=pre_gen, timeout=0.5
        )
        post_gen = fresh_snapshot.generation if fresh_snapshot else action_result.post_generation
    else:
        post_gen = action_result.post_generation

    report = verify_action_result(action_result)
    payload = {
        "ok": action_result.ok,
        "action": action_result.action,
        "ref_id": action_result.ref_id,
        "action_id": action_result.action_id,
        "message": action_result.message,
        "pre_generation": action_result.pre_generation,
        "post_generation": post_gen,
        "verification": {
            "success": report.success,
            "confidence": report.confidence,
            "checks_passed": report.checks_passed,
            "needs_replan": report.needs_replan,
        }
    }
    return json.dumps(payload, ensure_ascii=False)


@registry.register(
    name="browser_click_ref",
    description="Queue a click on a stable browser element ref. Use only after browser_find/browser_describe_ref identifies the target ref.",
    parameters={
        "type": "object",
        "properties": {
            "ref_id": {"type": "string", "description": "Stable browser ref id"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["ref_id"]
    }
)
async def browser_click_ref(ref_id: str, session_id: str = "") -> str:
    if not ref_id.startswith("mw_"):
        return _error_payload(
            (
                f"Invalid ref_id '{ref_id}'. ref_ids have the format 'mw_N' and must come "
                f"from a browser_read_page() or browser_find() result. "
                f"Call browser_read_page() first to discover available elements and their ref_ids."
            ),
            action="click",
            ref_id=ref_id,
            session_id=session_id or "",
            error_code="invalid_ref_format",
        )
    return await _queue_browser_action("click", ref_id, session_id=session_id)


@registry.register(
    name="browser_type_ref",
    description="Queue typing text into a stable browser input ref. Supports clearing the field first.",
    parameters={
        "type": "object",
        "properties": {
            "ref_id": {"type": "string", "description": "Stable browser ref id"},
            "text": {"type": "string", "description": "Text to type"},
            "clear_first": {"type": "boolean", "description": "Clear existing value first"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["ref_id", "text"]
    }
)
async def browser_type_ref(ref_id: str, text: str, clear_first: bool = False, session_id: str = "") -> str:
    if not ref_id.startswith("mw_"):
        return _error_payload(
            (
                f"Invalid ref_id '{ref_id}'. ref_ids have the format 'mw_N' and must come "
                f"from a browser_read_page() or browser_click_match() result. "
                f"Call browser_read_page() first to discover available elements and their ref_ids."
            ),
            action="type",
            ref_id=ref_id,
            session_id=session_id or "",
            error_code="invalid_ref_format",
        )
    return await _queue_browser_action("type", ref_id, session_id=session_id, text=text, clear_first=clear_first)


@registry.register(
    name="browser_select_ref",
    description="Queue selecting an option on a stable browser select/combobox ref.",
    parameters={
        "type": "object",
        "properties": {
            "ref_id": {"type": "string", "description": "Stable browser ref id"},
            "option": {"type": "string", "description": "Option label or value to select"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["ref_id", "option"]
    }
)
async def browser_select_ref(ref_id: str, option: str, session_id: str = "") -> str:
    if not ref_id.startswith("mw_"):
        return _error_payload(
            (
                f"Invalid ref_id '{ref_id}'. ref_ids have the format 'mw_N' and must come "
                f"from a browser_read_page() or browser_find() result. "
                f"Call browser_read_page() first to discover available elements and their ref_ids."
            ),
            action="select",
            ref_id=ref_id,
            session_id=session_id or "",
            error_code="invalid_ref_format",
        )
    return await _queue_browser_action("select", ref_id, session_id=session_id, option=option)


@registry.register(
    name="browser_refresh_refs",
    description="Request a fresh browser snapshot from the extension and wait briefly for a new generation when the bridge is connected. Falls back to latest known state if no refresh arrives.",
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Optional browser session id"},
            "timeout": {"type": "number", "description": "Seconds to wait for a refreshed snapshot", "default": 2.0}
        },
        "required": []
    }
)
async def browser_refresh_refs(session_id: str = "", timeout: float = 2.0) -> str:
    snapshot = _lookup_snapshot(session_id)
    resolved_session_id = session_id or browser_bridge.connected_session_id() or (snapshot.session_id if snapshot else "")

    if not snapshot:
        if not browser_bridge.is_connected() or not resolved_session_id:
            _, error = await _require_snapshot(session_id)
            return error

        refresh_request = ActionRequest(
            action="refresh_snapshot",
            ref_id="",
            session_id=resolved_session_id,
            timeout=timeout,
        )
        queued = browser_bridge.queue_action(refresh_request)
        if queued.ok:
            current = await browser_bridge.wait_for_snapshot(
                session_id=resolved_session_id, min_generation=0, timeout=max(0.1, timeout)
            )
            if current:
                return json.dumps({
                    "ok": True,
                    "session_id": current.session_id or resolved_session_id,
                    "generation": current.generation,
                    "elements": len(current.elements),
                    "message": "Browser snapshot bootstrapped from extension.",
                    **_snapshot_health(current),
                }, ensure_ascii=False)
        return _error_payload(
            "ERROR: No active browser snapshot is available. The Chrome extension bridge must publish a snapshot first.",
            error_code="no_snapshot",
            session_id=resolved_session_id,
        )

    previous_generation = snapshot.generation
    if browser_bridge.is_connected():
        refresh_request = ActionRequest(action="refresh_snapshot", ref_id="", session_id=snapshot.session_id, timeout=timeout)
        queued = browser_bridge.queue_action(refresh_request)
        if queued.ok:
            current = await browser_bridge.wait_for_snapshot(
                session_id=snapshot.session_id,
                min_generation=previous_generation,
                timeout=max(0.1, timeout),
            )
            if current and current.generation != previous_generation:
                snapshot = current
                return json.dumps({
                    "ok": True,
                    "session_id": snapshot.session_id,
                    "generation": snapshot.generation,
                    "elements": len(snapshot.elements),
                    "message": "Browser snapshot refreshed from extension.",
                    **_snapshot_health(snapshot),
                }, ensure_ascii=False)
            snapshot = current or browser_store.get_snapshot(snapshot.session_id) or snapshot
    return json.dumps({
        "ok": True,
        "session_id": snapshot.session_id,
        "generation": snapshot.generation,
        "elements": len(snapshot.elements),
        "message": "Using latest known browser snapshot generation.",
        **_snapshot_health(snapshot),
    }, ensure_ascii=False)


@registry.register(
    name="browser_scroll",
    description=(
        "Scroll the current browser page. Use this instead of press_key for scrolling web pages. "
        "Returns the new scroll position, page height, and whether you've reached the bottom. "
        "After scrolling, a fresh DOM snapshot is automatically captured."
    ),
    parameters={
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "description": "Scroll direction",
                "enum": ["down", "up", "top", "bottom"]
            },
            "amount": {
                "type": "string",
                "description": "How much to scroll: 'page' (85% viewport), 'half' (50% viewport), or pixel count like '300'",
                "default": "page"
            },
            "session_id": {"type": "string", "description": "Optional browser session id"},
        },
        "required": ["direction"]
    }
)
async def browser_scroll(direction: str = "down", amount: str = "page", session_id: str = "") -> str:
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return _error_payload(
            error,
            action="scroll",
            direction=direction,
            amount=amount,
            session_id=session_id or "",
            error_code="no_snapshot",
        )

    if not browser_bridge.is_connected():
        return _error_payload(
            "Browser extension bridge is not connected. Cannot scroll.",
            action="scroll",
            direction=direction,
            amount=amount,
            session_id=session_id or snapshot.session_id,
            error_code="bridge_disconnected",
        )

    resolved_session_id = session_id or snapshot.session_id

    # Queue a scroll action via the bridge
    request = ActionRequest(
        action="scroll",
        ref_id="",
        session_id=resolved_session_id,
        timeout=3.0,
        metadata={
            "tab_id": snapshot.tab_id,
            "generation": str(snapshot.generation),
            "direction": direction,
            "amount": amount,
        },
    )
    result = browser_bridge.queue_action(request)
    if not result.ok:
        return _error_payload(
            result.message,
            action="scroll",
            direction=direction,
            amount=amount,
            session_id=resolved_session_id,
            error_code="queue_failed",
        )

    # Wait for the scroll action result and fresh snapshot
    timeout_s = 3.0
    action_result = await browser_bridge.wait_for_result(result.action_id, timeout=timeout_s)
    if action_result:
        # Wait briefly for fresh snapshot after scroll
        fresh_snapshot = await browser_bridge.wait_for_snapshot(
            session_id=resolved_session_id,
            min_generation=snapshot.generation,
            timeout=0.5,
        )
        vp = fresh_snapshot.viewport if fresh_snapshot else snapshot.viewport
        page_height = getattr(vp, 'page_height', 0) or getattr(vp, 'scroll_height', 0) or 0
        scroll_y = vp.scroll_y
        viewport_height = vp.height
        at_bottom = (scroll_y + viewport_height) >= (page_height - 5) if page_height > 0 else False

        return json.dumps({
            "ok": True,
            "direction": direction,
            "amount": amount,
            "scroll_y": scroll_y,
            "page_height": page_height,
            "viewport_height": viewport_height,
            "at_bottom": at_bottom,
            "at_top": scroll_y <= 0,
            "message": f"Scrolled {direction} by {amount}. Position: {scroll_y}/{page_height}px."
                       + (" Reached bottom of page." if at_bottom else ""),
            "generation": fresh_snapshot.generation if fresh_snapshot else snapshot.generation,
        }, ensure_ascii=False)

    # Timeout — return best-effort from viewport data
    fresh_snapshot = browser_store.get_snapshot(resolved_session_id) or snapshot
    vp = fresh_snapshot.viewport
    page_height = getattr(vp, 'page_height', 0) or getattr(vp, 'scroll_height', 0) or 0
    return json.dumps({
        "ok": True,
        "direction": direction,
        "amount": amount,
        "scroll_y": vp.scroll_y,
        "page_height": page_height,
        "viewport_height": vp.height,
        "at_bottom": (vp.scroll_y + vp.height) >= (page_height - 5) if page_height > 0 else False,
        "at_top": vp.scroll_y <= 0,
        "message": f"Scroll {direction} queued. Snapshot may still be updating.",
        "generation": fresh_snapshot.generation,
    }, ensure_ascii=False)


@registry.register(
    name="browser_wait_for",
    description="Check whether a browser expectation is already satisfied in the latest snapshot. This is a Phase 1 verification helper until extension-side wait execution is added.",
    parameters={
        "type": "object",
        "properties": {
            "expectation": {"type": "string", "description": "Expected text, URL fragment, or title fragment"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["expectation"]
    }
)
async def browser_wait_for(expectation: str, session_id: str = "") -> str:
    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return error

    haystack = " ".join([snapshot.url, snapshot.title] + [el.primary_label() for el in snapshot.elements[:200]]).lower()
    ok = expectation.lower() in haystack
    return json.dumps({
        "ok": ok,
        "expectation": expectation,
        "generation": snapshot.generation,
        "message": "Expectation present in latest snapshot." if ok else "Expectation not yet present in latest snapshot.",
    }, ensure_ascii=False)


@registry.register(
    name="browser_assert",
    description="Assert that a browser expectation matches the latest snapshot state. Use for strict task verification before marking a browser todo done.",
    parameters={
        "type": "object",
        "properties": {
            "expectation": {"type": "string", "description": "Expected text, URL fragment, title fragment, or element label"},
            "session_id": {"type": "string", "description": "Optional browser session id"}
        },
        "required": ["expectation"]
    }
)
async def browser_assert(expectation: str, session_id: str = "") -> str:
    return await browser_wait_for(expectation, session_id=session_id)


# ═══════════════════════════════════════════════════════════════
#  Tab Awareness Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="browser_list_tabs",
    description=(
        "List all known open browser tabs with their URLs and titles. "
        "Use this BEFORE opening a new URL to check if it's already open. "
        "Returns tabs most-recently-seen first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional filter — only return tabs whose URL or title contains this substring"
            }
        },
        "required": []
    }
)
async def browser_list_tabs(query: str = "") -> str:
    # First try to refresh the tab list from Chrome via AppleScript
    await _refresh_chrome_tabs()

    tabs = browser_store.get_tabs()
    if not tabs:
        return json.dumps({"tabs": [], "count": 0, "message": "No known open browser tabs."})

    query_lower = query.lower().strip() if query else ""
    filtered = []
    for tab in tabs:
        if query_lower:
            if query_lower not in tab.url.lower() and query_lower not in tab.title.lower() and query_lower not in tab.domain.lower():
                continue
        filtered.append({
            "tab_id": tab.tab_id,
            "url": tab.url,
            "title": tab.title,
            "domain": tab.domain,
            "age_seconds": round(time.time() - tab.last_seen, 1),
        })

    return json.dumps({
        "tabs": filtered[:20],
        "count": len(filtered),
        "total_tabs": len(tabs),
    }, ensure_ascii=False)


@registry.register(
    name="browser_switch_tab",
    description=(
        "Switch to an already-open browser tab by its URL or domain. "
        "Use this instead of open_url when the page is already open to avoid duplicate tabs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL or domain to switch to (matches against open tabs)"
            }
        },
        "required": ["url"]
    }
)
async def browser_switch_tab(url: str) -> str:
    from tools.registry import _osascript

    # Check tab ledger
    tab = browser_store.find_tab_by_url(url) or browser_store.find_tab_by_domain(url)
    if not tab:
        return json.dumps({"ok": False, "message": f"No open tab matches '{url}'. Use open_url to open it."})

    # Try to switch to the tab via AppleScript
    result = await _switch_to_chrome_tab(tab.url)
    if result:
        return json.dumps({
            "ok": True,
            "message": f"Switched to tab: {tab.title or tab.url}",
            "tab": {"tab_id": tab.tab_id, "url": tab.url, "title": tab.title},
        })
    return json.dumps({
        "ok": False,
        "message": f"Found tab '{tab.title}' but couldn't switch to it. Try open_url instead.",
    })


async def _refresh_chrome_tabs():
    """Query Chrome for all open tabs and register them in the store."""
    from tools.registry import _osascript

    try:
        # Get all tab URLs
        urls_raw = await _osascript(
            'tell application "Google Chrome" to set tabURLs to {}\n'
            'tell application "Google Chrome"\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      set end of tabURLs to URL of t\n'
            '    end repeat\n'
            '  end repeat\n'
            '  return tabURLs\n'
            'end tell'
        )
        # Get all tab titles
        titles_raw = await _osascript(
            'tell application "Google Chrome" to set tabTitles to {}\n'
            'tell application "Google Chrome"\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      set end of tabTitles to title of t\n'
            '    end repeat\n'
            '  end repeat\n'
            '  return tabTitles\n'
            'end tell'
        )

        if not urls_raw:
            return

        urls = [u.strip() for u in urls_raw.split(", ") if u.strip()]
        titles = [t.strip() for t in titles_raw.split(", ")] if titles_raw else []

        tab_list = []
        for i, url in enumerate(urls):
            title = titles[i] if i < len(titles) else ""
            tab_list.append({
                "tab_id": f"chrome_tab_{i}",
                "index": i,
                "url": url,
                "title": title,
            })
        browser_store.register_external_tabs(tab_list)
    except Exception:
        pass  # Chrome might not be running


async def _switch_to_chrome_tab(target_url: str) -> bool:
    """Switch Chrome to the tab matching target_url. Returns True on success."""
    from tools.registry import _osascript

    try:
        # AppleScript to find and activate the matching tab
        escaped_url = target_url.replace('"', '\\"')
        script = f'''
tell application "Google Chrome"
    activate
    set targetURL to "{escaped_url}"
    repeat with w in windows
        set tabIndex to 0
        repeat with t in tabs of w
            set tabIndex to tabIndex + 1
            if URL of t starts with targetURL or targetURL starts with URL of t then
                set active tab index of w to tabIndex
                set index of w to 1
                return "switched"
            end if
        end repeat
    end repeat
    return "not_found"
end tell
'''
        result = await _osascript(script)
        return "switched" in result.lower() if result else False
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  Browser Image Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="browser_copy_image",
    description=(
        "Copy an image from the current browser page to the macOS clipboard. "
        "Specify the image by its ref_id (from browser_read_page) or by a query "
        "describing which image to copy. The image is downloaded and placed on "
        "the clipboard as image data, ready for pasting into any app.\n"
        "Workflow: browser_read_page → find image ref → browser_copy_image → clipboard_ops(paste)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "ref_id": {
                "type": "string",
                "description": "Ref ID of the image element (from browser_read_page)"
            },
            "query": {
                "type": "string",
                "description": "Alternatively, describe the image: 'the chart', 'company logo', 'product photo'"
            },
            "session_id": {
                "type": "string",
                "description": "Optional browser session id"
            }
        },
        "required": []
    }
)
async def browser_copy_image(ref_id: str = "", query: str = "", session_id: str = "") -> str:
    import urllib.request
    import tempfile

    snapshot, error = await _require_snapshot(session_id)
    if not snapshot:
        return error

    image_url = ""
    image_label = ""

    # Resolve by ref_id
    if ref_id:
        element = _resolve_element(ref_id, session_id)
        if not element:
            return f"ERROR: Unknown browser ref '{ref_id}'."
        # Get the image source URL
        image_url = element.href or ""
        # Check if it's an <img> tag — the src is typically in href or value
        if element.tag == "img":
            image_url = element.href or element.value or ""
        image_label = element.primary_label()

    # Resolve by query — find image elements matching the description
    if not image_url and query:
        query_norm = _norm(query)
        best_element = None
        best_score = -1

        for el in snapshot.elements:
            if not el.visible:
                continue
            tag = (el.tag or "").lower()
            role = (el.role or "").lower()
            label = _norm(el.primary_label())
            context = _norm(el.context_text)

            # Must be an image-like element
            is_image = tag == "img" or role == "img" or "image" in role
            has_src = bool(el.href or el.value)

            if not is_image and not has_src:
                continue

            score = 0
            if is_image:
                score += 50
            if has_src:
                score += 30
            if query_norm in label or query_norm in context:
                score += 100
            # Partial word matches
            for word in query_norm.split():
                if word in label or word in context:
                    score += 20

            if score > best_score:
                best_score = score
                best_element = el

        if best_element and best_score > 30:
            image_url = best_element.href or best_element.value or ""
            image_label = best_element.primary_label()
            ref_id = best_element.ref_id
        else:
            return f"ERROR: No image found matching query '{query}'. Use browser_read_page to see available elements."

    if not image_url:
        # Fallback: try to extract src via JS on the ref
        if ref_id:
            from tools.registry import _osascript
            js_result = await _get_image_src_via_js(ref_id, snapshot)
            if js_result:
                image_url = js_result

    if not image_url:
        return f"ERROR: Could not determine image URL for ref '{ref_id}'. The element may not have a src/href attribute."

    # Make URL absolute if relative
    if image_url.startswith("//"):
        image_url = "https:" + image_url
    elif image_url.startswith("/"):
        from urllib.parse import urlparse
        parsed = urlparse(snapshot.url)
        image_url = f"{parsed.scheme}://{parsed.netloc}{image_url}"

    # Download the image
    try:
        from tools.mac_tools import save_image, _set_image_clipboard
        save_result = await save_image(image_url)
        save_data = json.loads(save_result)
        if not save_data.get("ok"):
            return f"ERROR: Could not download image: {save_result}"

        image_path = save_data["path"]

        # Load onto clipboard
        clip_result = await _set_image_clipboard(image_path)
        if "ERROR" in clip_result:
            return clip_result

        return json.dumps({
            "ok": True,
            "ref_id": ref_id,
            "image_url": image_url,
            "image_label": image_label,
            "local_path": image_path,
            "message": f"Image '{image_label or 'untitled'}' copied to clipboard. "
                       f"Use clipboard_ops(action='paste') to paste it."
        })
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


async def _get_image_src_via_js(ref_id: str, snapshot) -> str:
    """Try to get the src attribute of an img element via the browser extension DOM path."""
    element = browser_store.get_element(ref_id, snapshot.session_id)
    if not element:
        return ""
    # If we have the href already, use it
    if element.href and ("http" in element.href or element.href.startswith("//")):
        return element.href
    # Check value field
    if element.value and ("http" in element.value or element.value.startswith("//")):
        return element.value
    return ""
