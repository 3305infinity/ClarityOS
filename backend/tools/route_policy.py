"""
Moonwalk — Deterministic web route policy.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def _domain(url: str) -> str:
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


_SEARCH_RESULT_HOSTS: frozenset[str] = frozenset({
    "google.com", "bing.com", "duckduckgo.com", "search.yahoo.com",
})


def _is_search_results_url(url: str) -> bool:
    if not url:
        return False
    host = _domain(url)
    if host not in _SEARCH_RESULT_HOSTS:
        return False
    lowered = url.lower()
    return "/search" in lowered or "q=" in lowered


def decide_web_route(
    *,
    target_type: str,
    query: str,
    url: str,
    item_hint: str,
    context: dict[str, Any],
    runtime_state: dict[str, Any],
) -> dict[str, Any]:
    browser_state = (runtime_state or {}).get("browser_state", {}) or {}
    os_state = (runtime_state or {}).get("os_state", {}) or {}
    current_url = str(browser_state.get("url") or context.get("browser_url") or os_state.get("browser_url") or "").strip()
    background_mode = bool(context.get("background_mode"))
    active_app = str(context.get("active_app") or os_state.get("active_app") or "").strip().lower()
    target_type = str(target_type or "").strip().lower()
    query_lower = str(query or "").strip().lower()
    explicit_url = str(url or "").strip()
    live_browser_connected = bool(browser_state.get("connected"))

    if background_mode:
        return {
            "route": "background_fetch",
            "reason": "Background mode disables live browser routing.",
            "confidence": 0.99,
            "policy": "deterministic-web-policy",
        }

    if explicit_url:
        if current_url and explicit_url.rstrip("/") == current_url.rstrip("/"):
            return {
                "route": "browser_aci",
                "reason": "Explicit URL matches the live browser page.",
                "confidence": 0.98,
                "policy": "deterministic-web-policy",
            }
        # When browser is connected, open the URL in the live browser so
        # the user can see the navigation happen in real-time.
        if live_browser_connected:
            return {
                "route": "browser_aci",
                "reason": "Live browser connected — opening URL visibly instead of background fetch.",
                "confidence": 0.94,
                "policy": "deterministic-web-policy",
            }
        return {
            "route": "background_fetch",
            "reason": "No live browser — explicit URLs fall back to background reads.",
            "confidence": 0.95,
            "policy": "deterministic-web-policy",
        }

    deictic_markers = ("current page", "this page", "current tab", "this tab", "on this site", "open tab")
    if any(marker in query_lower for marker in deictic_markers):
        return {
            "route": "browser_aci" if live_browser_connected else "background_fetch",
            "reason": "The request refers to the live browser page/tab.",
            "confidence": 0.96 if live_browser_connected else 0.65,
            "policy": "deterministic-web-policy",
        }

    if target_type == "search_results":
        # When browser is connected, do live Google search so the user sees it
        if live_browser_connected:
            return {
                "route": "browser_aci",
                "reason": "Live browser connected — searching visibly in the browser.",
                "confidence": 0.93,
                "policy": "deterministic-web-policy",
            }
        return {
            "route": "background_fetch",
            "reason": "No live browser — search results via background search.",
            "confidence": 0.93,
            "policy": "deterministic-web-policy",
        }

    browser_apps = {"google chrome", "chrome", "safari", "arc", "brave", "brave browser", "firefox", "edge", "microsoft edge"}

    # When browser is active and on a search page, boost confidence for ACI
    _is_serp = _is_search_results_url(current_url) if current_url else False
    if (
        live_browser_connected
        and active_app in browser_apps
        and target_type in {"page_summary", "page_content", "structured_data"}
        and _is_serp
    ):
        return {
            "route": "browser_aci",
            "reason": "Browser is on a live search-results page; use ACI to extract and follow results.",
            "confidence": 0.97,
            "policy": "deterministic-web-policy",
        }

    dynamic_markers = ("sign in", "login", "dashboard", "console", "settings", "workspace")
    browser_domain = _domain(current_url)
    if live_browser_connected and browser_domain and any(marker in query_lower for marker in dynamic_markers):
        return {
            "route": "browser_aci",
            "reason": "The request likely targets a dynamic or authenticated browser workflow.",
            "confidence": 0.85,
            "policy": "deterministic-web-policy",
        }

    # ── Default: prefer live browser when connected ──
    if live_browser_connected:
        return {
            "route": "browser_aci",
            "reason": "Live browser connected — routing through visible browser for real-time feedback.",
            "confidence": 0.90,
            "policy": "deterministic-web-policy",
        }

    return {
        "route": "background_fetch",
        "reason": "No live browser connected — using background search and read.",
        "confidence": 0.9,
        "policy": "deterministic-web-policy",
    }
