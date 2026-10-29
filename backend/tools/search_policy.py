"""
Moonwalk — Deterministic search result ranking and following.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse


_AUTHORITY_HINTS = (
    "changelog",
    "release-notes",
    "release notes",
    "updates",
    "update",
    "docs",
    "documentation",
    "blog",
    "help",
    "support",
)

_NOISE_HOSTS = {
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "reddit.com",
    "pinterest.com",
}


def _domain(url: str) -> str:
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def score_search_item(query: str, item: dict[str, Any], *, target_type: str = "") -> float:
    query_norm = _norm(query)
    query_tokens = {token for token in query_norm.split() if token}
    href = str(item.get("href", "") or "").strip()
    label = _norm(str(item.get("label", "") or ""))
    context = _norm(str(item.get("context", "") or ""))
    domain = _domain(href)
    text = " ".join(part for part in (label, context, href.lower()) if part)

    score = 0.0
    if query_norm and label == query_norm:
        score += 120.0
    if query_norm and query_norm in text:
        score += 60.0
    overlap = query_tokens & set(text.split())
    score += float(len(overlap) * 10)

    if domain and domain not in _NOISE_HOSTS:
        score += 8.0
    if domain in _NOISE_HOSTS:
        score -= 15.0

    for hint in _AUTHORITY_HINTS:
        if hint in href.lower() or hint in label or hint in context:
            score += 18.0

    if target_type == "page_summary" and any(hint in href.lower() for hint in ("changelog", "release", "update", "docs")):
        score += 20.0

    if label.startswith("about this result") or label.startswith("images"):
        score -= 40.0
    if "search?" in href.lower() or "/search" in href.lower():
        score -= 35.0

    return score


def choose_search_result(
    items: list[dict[str, Any]],
    *,
    query: str,
    target_type: str,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        score = score_search_item(query, item, target_type=target_type)
        if score <= 0:
            continue
        ranked.append((score, item))

    if not ranked:
        return (items[0] if items else None), {
            "search_follow_strategy": "deterministic-first-result",
            "search_follow_degraded": True,
            "search_follow_reason": "No strong deterministic winner; using first result.",
            "search_follow_confidence": 0.35,
        }

    ranked.sort(key=lambda row: row[0], reverse=True)
    best_score, best_item = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    confidence = 0.92 if best_score - second_score >= 18 else 0.72
    return best_item, {
        "search_follow_strategy": "deterministic-ranking",
        "search_follow_degraded": False,
        "search_follow_reason": "Followed the top-ranked deterministic search result.",
        "search_follow_confidence": round(confidence, 2),
        "search_follow_model": "deterministic-search-policy",
        "search_follow_score": round(best_score, 2),
    }
