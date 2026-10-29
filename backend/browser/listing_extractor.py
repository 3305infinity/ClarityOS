"""Deterministic extraction of repeated property/listing cards from browser snapshots."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


_PRICE_RE = re.compile(r"(£\s?[\d,]+(?:\.\d{2})?\s*(?:pcm|pw|per month|per week)?)", re.I)
_BED_RE = re.compile(r"(\d+)\s*bed", re.I)
_BATH_RE = re.compile(r"(\d+)\s*bath", re.I)
_RECEPTION_RE = re.compile(r"(\d+)\s*reception", re.I)
_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", re.I)
_PROPERTY_DETAIL_HINTS = (
    "/to-rent/details/",
    "/for-sale/details/",
    "/property/",
    "/properties/",
    "/details/",
)
_PROPERTY_QUERY_TERMS = {
    "apartment",
    "apartments",
    "flat",
    "flats",
    "property",
    "properties",
    "home",
    "homes",
    "rental",
    "rent",
    "rentals",
    "studio",
}


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _domain(url: str) -> str:
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _normalize_href(snapshot_url: str, href: str) -> str:
    href = str(href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        parsed = urlparse(snapshot_url or "")
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{href}"
    return href


def _looks_like_property_page(snapshot_url: str, query: str) -> bool:
    domain = _domain(snapshot_url)
    lowered_url = str(snapshot_url or "").lower()
    query_terms = set(_norm(query).split())
    if domain in {"zoopla.co.uk", "rightmove.co.uk", "onthemarket.com"}:
        return True
    if any(token in lowered_url for token in ("to-rent", "for-sale", "property", "properties", "apartments", "flats")):
        return True
    return bool(query_terms.intersection(_PROPERTY_QUERY_TERMS))


def _parse_listing_fields(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    price = _PRICE_RE.search(text or "")
    if price:
        fields["price"] = price.group(1).strip()
    beds = _BED_RE.search(text or "")
    if beds:
        fields["bedrooms"] = int(beds.group(1))
    baths = _BATH_RE.search(text or "")
    if baths:
        fields["bathrooms"] = int(baths.group(1))
    receptions = _RECEPTION_RE.search(text or "")
    if receptions:
        fields["receptions"] = int(receptions.group(1))
    postcode = _POSTCODE_RE.search(text or "")
    if postcode:
        fields["postcode"] = postcode.group(0).strip()
    return fields


def _score_listing(snapshot_url: str, href: str, text: str) -> float:
    score = 0.0
    parsed = _parse_listing_fields(text)
    if parsed.get("price"):
        score += 45.0
    if "bedrooms" in parsed:
        score += 20.0
    if "bathrooms" in parsed:
        score += 12.0
    if "receptions" in parsed:
        score += 8.0
    if parsed.get("postcode"):
        score += 12.0
    lowered = _norm(text)
    if any(term in lowered for term in ("egham", "englefield green", "surrey", "tw20")):
        score += 16.0
    if any(hint in href.lower() for hint in _PROPERTY_DETAIL_HINTS):
        score += 26.0
    if _domain(snapshot_url) in {"zoopla.co.uk", "rightmove.co.uk", "onthemarket.com"}:
        score += 10.0
    if len(text.strip()) >= 60:
        score += 6.0
    return score


def extract_property_listing_items(snapshot: Any, *, query: str = "", max_items: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract listing-like cards from visible browser elements."""
    if not snapshot or not getattr(snapshot, "elements", None):
        return [], {"extraction_strategy": "deterministic-property-cards", "confidence": 0.0}

    if not _looks_like_property_page(getattr(snapshot, "url", ""), query):
        return [], {"extraction_strategy": "deterministic-property-cards", "confidence": 0.0}

    grouped: dict[str, dict[str, Any]] = {}
    for el in snapshot.elements:
        if not getattr(el, "visible", True):
            continue
        href = _normalize_href(getattr(snapshot, "url", ""), getattr(el, "href", ""))
        text = " | ".join(
            part for part in (
                getattr(el, "text", "") or getattr(el, "aria_label", "") or getattr(el, "name", "") or "",
                getattr(el, "context_text", "") or "",
            ) if part
        ).strip()
        if len(text) < 20:
            continue
        score = _score_listing(getattr(snapshot, "url", ""), href, text)
        if score < 45.0:
            continue
        key = href or f"ref:{getattr(el, 'ref_id', '')}"
        existing = grouped.get(key)
        parsed = _parse_listing_fields(text)
        item = {
            "ref_id": str(getattr(el, "ref_id", "") or "").strip(),
            "label": (getattr(el, "text", "") or getattr(el, "primary_label", lambda: "")() or text).strip()[:200],
            "context": (getattr(el, "context_text", "") or text).strip()[:240],
            "href": href,
            "href_domain": _domain(href or getattr(snapshot, "url", "")),
            "role": (getattr(el, "role", "") or "").lower(),
            "tag": (getattr(el, "tag", "") or "").lower(),
            "actions": list(getattr(el, "action_types", []) or []),
            "score": score,
        }
        item.update(parsed)
        if existing is None or score > float(existing.get("score", 0.0)):
            grouped[key] = item

    items = sorted(grouped.values(), key=lambda item: float(item.get("score", 0.0)), reverse=True)
    for idx, item in enumerate(items[: max(1, min(int(max_items or 20), 20))], start=1):
        item["rank"] = idx

    final_items = items[: max(1, min(int(max_items or 20), 20))]
    confidence = 0.92 if len(final_items) >= 4 else 0.82 if len(final_items) >= 2 else 0.66 if final_items else 0.0
    return final_items, {
        "extraction_strategy": "deterministic-property-cards",
        "source_domain": _domain(getattr(snapshot, "url", "")),
        "confidence": confidence,
        "item_count": len(final_items),
    }
