"""Flash-backed browser page interpretation helpers."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Tuple

from providers.gemini import GeminiProvider
from providers.models import fast_model


_FLASH_TIMEOUT_SECONDS = 14.0
_provider_cache: Dict[Tuple[str, str], GeminiProvider] = {}


class BrowserInterpretationError(Exception):
    """Raised when the Flash browser interpreter cannot produce a valid result."""

    def __init__(self, reason: str, *, error_code: str = "flash_interpreter_error"):
        super().__init__(reason)
        self.reason = reason
        self.error_code = error_code


def _get_gemini_provider(api_key: str, model_name: str) -> GeminiProvider:
    cache_key = (api_key, model_name)
    provider = _provider_cache.get(cache_key)
    if provider is None:
        provider = GeminiProvider(api_key=api_key, model=model_name)
        _provider_cache[cache_key] = provider
    return provider


def _norm(text: str) -> str:
    return " ".join((text or "").strip().split())


def _serialize_elements(snapshot, limit: int = 140) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for el in snapshot.elements:
        if not getattr(el, "visible", True):
            continue
        label = _norm(el.primary_label())
        href = _norm(getattr(el, "href", ""))
        context = _norm(getattr(el, "context_text", ""))
        if not any((label, href, context)):
            continue
        serialized.append(
            {
                "ref_id": el.ref_id,
                "agent_id": int(getattr(el, "agent_id", 0) or 0),
                "label": label[:220],
                "role": _norm(getattr(el, "role", "") or getattr(el, "tag", "")),
                "tag": _norm(getattr(el, "tag", "")),
                "href": href[:240],
                "context": context[:180],
                "actions": list(getattr(el, "action_types", []) or []),
                "enabled": bool(getattr(el, "enabled", True)),
                "selected": bool(getattr(el, "selected", False)),
                "checked": bool(getattr(el, "checked", False)),
                "in_viewport": bool(getattr(el, "in_viewport", True)),
            }
        )
        if len(serialized) >= max(20, int(limit or 140)):
            break
    return serialized


def _extract_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise BrowserInterpretationError("Flash browser interpreter returned an empty response.", error_code="flash_empty")
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        payload = json.loads(raw[start : end + 1] if start != -1 and end != -1 else raw)
    except Exception as exc:
        raise BrowserInterpretationError(
            f"Flash browser interpreter returned invalid JSON: {exc}",
            error_code="flash_invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise BrowserInterpretationError(
            "Flash browser interpreter returned a non-object payload.",
            error_code="flash_invalid_payload",
        )
    return payload


async def _generate_with_flash(*, system_prompt: str, user_payload: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model_name = fast_model()

    if not api_key:
        raise BrowserInterpretationError(
            "Flash browser interpreter unavailable: GEMINI_API_KEY is not configured.",
            error_code="flash_unavailable",
        )

    provider = _get_gemini_provider(api_key=api_key, model_name=model_name)
    if not await provider.is_available():
        raise BrowserInterpretationError(
            f"Flash browser interpreter unavailable: provider '{model_name}' is not available.",
            error_code="flash_unavailable",
        )

    try:
        response = await asyncio.wait_for(
            provider.generate(
                messages=[{"role": "user", "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}],
                system_prompt=system_prompt,
                tools=[],
                temperature=0.0,
            ),
            timeout=_FLASH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise BrowserInterpretationError(
            f"Flash browser interpreter timed out after {_FLASH_TIMEOUT_SECONDS:.1f}s.",
            error_code="flash_timeout",
        ) from exc

    if getattr(response, "error", ""):
        raise BrowserInterpretationError(
            f"Flash browser interpreter returned an error: {response.error}",
            error_code="flash_error",
        )
    payload = _extract_json(getattr(response, "text", "") or "")
    payload["_interpreter_model"] = model_name
    return payload


async def summarize_page_with_flash(snapshot) -> Dict[str, Any]:
    elements = _serialize_elements(snapshot)
    if not elements:
        raise BrowserInterpretationError(
            "No visible browser elements are available for Flash page interpretation.",
            error_code="no_visible_elements",
        )

    system_prompt = (
        "You are Moonwalk's browser page interpreter. Analyze the provided browser snapshot and return ONLY JSON. "
        "Use only the provided elements and ref_ids. Do not invent information. "
        "Classify the page, summarize it, and identify a few important headings or targets.\n\n"
        "Return JSON with this schema:\n"
        "{"
        "\"page_type\": string,"
        "\"summary\": string,"
        "\"headings\": [{\"ref_id\": string, \"text\": string, \"tag\": string}],"
        "\"key_targets\": [{\"ref_id\": string, \"label\": string, \"reason\": string}],"
        "\"confidence\": number"
        "}"
    )
    payload = await _generate_with_flash(
        system_prompt=system_prompt,
        user_payload={
            "mode": "page_summary",
            "page": {
                "url": snapshot.url,
                "title": snapshot.title,
                "generation": snapshot.generation,
            },
            "viewport": {
                "scroll_y": getattr(snapshot.viewport, "scroll_y", 0),
                "height": getattr(snapshot.viewport, "height", 0),
                "page_height": getattr(snapshot.viewport, "page_height", 0) or getattr(snapshot.viewport, "scroll_height", 0) or 0,
            },
            "elements": elements,
        },
    )
    return payload


async def summarize_scraped_page_with_flash(
    *,
    url: str,
    title: str,
    content: str,
    links: List[Dict[str, Any]],
) -> Dict[str, Any]:
    content_text = (content or "").strip()
    if not content_text:
        raise BrowserInterpretationError(
            "No readable scraped content is available for background page summarization.",
            error_code="empty_content",
        )

    system_prompt = (
        "You are Moonwalk's background web page summarizer. Analyze the provided scraped page text and return ONLY JSON. "
        "Ignore cookie notices, navigation chrome, boilerplate menus, account prompts, and footer clutter unless they are central to the page. "
        "Produce a concise factual summary plus the most important section headings and follow-up targets.\n\n"
        "Return JSON with this schema:\n"
        "{"
        "\"page_type\": string,"
        "\"summary\": string,"
        "\"headings\": [{\"ref_id\": string, \"text\": string, \"tag\": string}],"
        "\"key_targets\": [{\"ref_id\": string, \"label\": string, \"href\": string, \"reason\": string}],"
        "\"confidence\": number"
        "}"
    )
    payload = await _generate_with_flash(
        system_prompt=system_prompt,
        user_payload={
            "mode": "background_page_summary",
            "page": {
                "url": url,
                "title": title,
            },
            "content": content_text[:10000],
            "links": [
                {
                    "label": _norm(str(link.get("label", "")))[:220],
                    "url": _norm(str(link.get("url", "")))[:300],
                }
                for link in (links or [])[:12]
                if isinstance(link, dict)
            ],
        },
    )
    return payload


async def extract_structured_items_with_flash(
    snapshot,
    *,
    item_type: str,
    query: str,
    max_items: int,
) -> Dict[str, Any]:
    elements = _serialize_elements(snapshot)
    if not elements:
        raise BrowserInterpretationError(
            "No visible browser elements are available for Flash structured extraction.",
            error_code="no_visible_elements",
        )

    system_prompt = (
        "You are Moonwalk's browser structured-data extractor. Analyze the provided browser snapshot and return ONLY JSON. "
        "Use only provided elements and ref_ids. Extract distinct items that best satisfy the requested item_type and query. "
        "Ignore page chrome, search navigation, utility actions, account controls, and 'About this result'-style noise unless the user explicitly asked for them.\n\n"
        "Return JSON with this schema:\n"
        "{"
        "\"page_type\": string,"
        "\"items\": [{\"ref_id\": string, \"label\": string, \"href\": string, \"context\": string, \"rank\": number, \"reason\": string}],"
        "\"notes\": string,"
        "\"confidence\": number"
        "}"
    )
    payload = await _generate_with_flash(
        system_prompt=system_prompt,
        user_payload={
            "mode": "structured_extract",
            "request": {
                "item_type": item_type,
                "query": query,
                "max_items": max(1, min(int(max_items or 20), 20)),
            },
            "page": {
                "url": snapshot.url,
                "title": snapshot.title,
                "generation": snapshot.generation,
            },
            "viewport": {
                "scroll_y": getattr(snapshot.viewport, "scroll_y", 0),
                "height": getattr(snapshot.viewport, "height", 0),
                "page_height": getattr(snapshot.viewport, "page_height", 0) or getattr(snapshot.viewport, "scroll_height", 0) or 0,
            },
            "elements": elements,
        },
    )
    return payload


async def decide_web_route_with_flash(
    *,
    target_type: str,
    query: str,
    url: str,
    item_hint: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    system_prompt = (
        "You are Moonwalk's web gateway route planner. Choose the best route for a web information request. "
        "Return ONLY JSON. Available routes are 'browser_aci' and 'background_fetch'. "
        "Prefer browser_aci when the task benefits from live browser interaction, live search result exploration, "
        "dynamic pages, or when browser context is clearly relevant. Prefer background_fetch for direct URL reads, "
        "static pages, or background research where browser interaction is unnecessary.\n\n"
        "Return JSON with this schema:\n"
        "{"
        "\"route\": \"browser_aci\" | \"background_fetch\","
        "\"reason\": string,"
        "\"confidence\": number"
        "}"
    )
    payload = await _generate_with_flash(
        system_prompt=system_prompt,
        user_payload={
            "mode": "route_decision",
            "request": {
                "target_type": target_type,
                "query": query,
                "url": url,
                "item_hint": item_hint,
            },
            "context": {
                "active_app": str(context.get("active_app", "")),
                "browser_url": str(context.get("browser_url", "")),
                "background_mode": bool(context.get("background_mode", False)),
                "browser_bridge_connected": bool(context.get("browser_bridge_connected", False)),
                "browser_has_snapshot": bool(context.get("browser_has_snapshot", False)),
                "browser_session_id": str(context.get("browser_session_id", "")),
            },
        },
    )
    route = str(payload.get("route", "")).strip()
    if route not in {"browser_aci", "background_fetch"}:
        raise BrowserInterpretationError(
            f"Flash web gateway selected invalid route '{route}'.",
            error_code="flash_invalid_route",
        )
    return payload


async def choose_search_result_with_flash(
    *,
    query: str,
    target_type: str,
    items: List[Dict[str, Any]],
    page_url: str = "",
    page_title: str = "",
) -> Dict[str, Any]:
    usable_items = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        href = _norm(str(item.get("href", "")))
        label = _norm(str(item.get("label", "")))
        context = _norm(str(item.get("context", "")))
        ref_id = _norm(str(item.get("ref_id", "")))
        if not any((href, label, context)):
            continue
        usable_items.append(
            {
                "ref_id": ref_id,
                "label": label[:220],
                "href": href[:320],
                "context": context[:220],
                "rank": int(item.get("rank", len(usable_items) + 1) or len(usable_items) + 1),
                "reason": _norm(str(item.get("reason", "")))[:220],
            }
        )
    if not usable_items:
        raise BrowserInterpretationError(
            "No usable search results are available for Flash result selection.",
            error_code="no_search_items",
        )

    system_prompt = (
        "You are Moonwalk's browser search-result chooser. Choose the single best result to follow next. "
        "Return ONLY JSON. Prefer authoritative, relevant sources. Use the provided items only.\n\n"
        "Return JSON with this schema:\n"
        "{"
        "\"selected_href\": string,"
        "\"selected_ref_id\": string,"
        "\"selected_label\": string,"
        "\"reason\": string,"
        "\"confidence\": number"
        "}"
    )
    payload = await _generate_with_flash(
        system_prompt=system_prompt,
        user_payload={
            "mode": "search_result_choice",
            "request": {
                "query": query,
                "target_type": target_type,
            },
            "page": {
                "url": page_url,
                "title": page_title,
            },
            "items": usable_items[:12],
        },
    )

    selected_href = _norm(str(payload.get("selected_href", "")))
    selected_ref_id = _norm(str(payload.get("selected_ref_id", "")))
    selected_label = _norm(str(payload.get("selected_label", "")))
    if not any((selected_href, selected_ref_id, selected_label)):
        raise BrowserInterpretationError(
            "Flash search-result chooser did not select a usable result.",
            error_code="flash_no_selection",
        )
    return payload
