"""Deterministic browser candidate selection helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .resolver import BrowserResolver
from .store import browser_store


_resolver = BrowserResolver()


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def query_implies_field(query: str) -> bool:
    query_norm = _norm(query)
    hints = [
        "field", "input", "textbox", "text box", "search box", "searchbar",
        "search bar", "search field", "combobox", "entry", "box",
    ]
    return any(hint in query_norm for hint in hints)


def merge_candidate_lists(*candidate_lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for candidate_list in candidate_lists:
        for candidate in candidate_list:
            ref_id = candidate.get("ref_id")
            if ref_id in seen:
                continue
            seen.add(ref_id)
            merged.append(candidate)
    merged.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return merged


def build_ranked_candidates(query: str, action: str, session_id: str = "", limit: int = 8) -> Tuple[Any, List[Dict[str, Any]], str]:
    snapshot = browser_store.get_snapshot(session_id or None)
    if not snapshot and session_id:
        snapshot = browser_store.get_snapshot(None)
    if not snapshot:
        return None, [], (
            "ERROR: No active browser snapshot is available. The Chrome extension bridge "
            "must connect and publish a page snapshot before browser ref tools can be used."
        )

    ranked = _resolver.describe_candidates(query, snapshot.elements, action=action, limit=max(1, min(limit, 10)))
    if action == "click" and query_implies_field(query):
        field_ranked = _resolver.describe_candidates(query, snapshot.elements, action="type", limit=max(1, min(limit, 10)))
        ranked = merge_candidate_lists(field_ranked, ranked)[: max(1, min(limit, 10))]
    return snapshot, ranked, ""


def _confidence_for_candidates(candidates: List[Dict[str, Any]]) -> tuple[float, bool]:
    if not candidates:
        return 0.0, True
    top = float(candidates[0].get("score", 0.0) or 0.0)
    second = float(candidates[1].get("score", 0.0) or 0.0) if len(candidates) > 1 else 0.0
    ambiguous = top <= 0 or (top - second) < 18.0
    if top >= 120 and not ambiguous:
        return 0.96, False
    if top >= 80 and not ambiguous:
        return 0.9, False
    return 0.68 if ambiguous else 0.82, ambiguous


async def select_browser_candidate_with_flash(
    query: str,
    action: str,
    session_id: str = "",
    text: str = "",
    option: str = "",
    limit: int = 8,
) -> Tuple[Dict[str, Any], str]:
    snapshot, ranked, error = build_ranked_candidates(query, action, session_id=session_id, limit=limit)
    if not snapshot:
        return {}, error
    if not ranked:
        return {}, f"No browser candidates matched query '{query}' for action '{action}'."

    confidence, ambiguous = _confidence_for_candidates(ranked)
    best = ranked[0]
    return {
        "ref_id": best["ref_id"],
        "reason": (
            "Deterministic resolver selected the highest-scoring candidate."
            if not ambiguous
            else "Deterministic resolver selected the best candidate, but the match is ambiguous."
        ),
        "model": "deterministic-resolver",
        "degraded_mode": ambiguous,
        "degraded_reason": "low_selection_confidence" if ambiguous else "",
        "confidence": confidence,
        "candidates": ranked,
    }, ""
