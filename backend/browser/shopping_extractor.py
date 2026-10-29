"""
Deterministic extraction of repeated product/shopping cards from browser snapshots.

Mirrors the listing_extractor pattern but targets e-commerce product pages:
Amazon, eBay, John Lewis, Argos, Currys, ASOS, Selfridges, etc.

Entry point: ``extract_shopping_items(snapshot, *, query, max_items)``
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


# ═══════════════════════════════════════════════════════════════
#  Regexes — product fields
# ═══════════════════════════════════════════════════════════════

_PRICE_RE = re.compile(
    r"((?:£|€|\$|USD|GBP|EUR)\s?[\d,]+(?:\.\d{1,2})?)", re.I
)
_RATING_RE = re.compile(
    r"(\d\.?\d?)\s*(?:out of|/)\s*5", re.I
)
_STAR_RE = re.compile(
    r"(\d\.?\d?)\s*star", re.I
)
_REVIEW_COUNT_RE = re.compile(
    r"([\d,]+)\s*(?:review|rating|customer)", re.I
)
_BRAND_RE = re.compile(
    r"(?:by|brand|from)\s+([A-Z][\w\s&'-]{1,30})", re.I
)
_DISCOUNT_RE = re.compile(
    r"(\d{1,3})\s*%\s*off", re.I
)
_WAS_PRICE_RE = re.compile(
    r"(?:was|rrp|save)\s*((?:£|€|\$)\s?[\d,]+(?:\.\d{1,2})?)", re.I
)

_PRODUCT_DETAIL_HINTS = (
    "/dp/",
    "/product/",
    "/products/",
    "/p/",
    "/itm/",
    "/item/",
    "/buy/",
    "/shop/",
    "/prd/",
)

_SHOPPING_DOMAINS = frozenset({
    "amazon.co.uk",
    "amazon.com",
    "ebay.co.uk",
    "ebay.com",
    "johnlewis.com",
    "argos.co.uk",
    "currys.co.uk",
    "asos.com",
    "selfridges.com",
    "next.co.uk",
    "very.co.uk",
    "hm.com",
    "zara.com",
    "uniqlo.com",
    "boots.com",
    "screwfix.com",
    "toolstation.com",
    "ikea.com",
    "wayfair.co.uk",
    "ao.com",
    "bestbuy.com",
    "target.com",
    "walmart.com",
    "etsy.com",
    "aliexpress.com",
    "notonthehighstreet.com",
})

_SHOPPING_QUERY_TERMS = {
    "buy",
    "shop",
    "shopping",
    "price",
    "deal",
    "deals",
    "offer",
    "offers",
    "product",
    "products",
    "compare",
    "cheapest",
    "best",
    "review",
    "reviews",
    "laptop",
    "phone",
    "shoes",
    "clothes",
    "furniture",
    "electronics",
}


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

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


def _looks_like_shopping_page(snapshot_url: str, query: str) -> bool:
    domain = _domain(snapshot_url)
    if domain in _SHOPPING_DOMAINS:
        return True
    lowered_url = str(snapshot_url or "").lower()
    if any(tok in lowered_url for tok in ("shop", "product", "buy", "category", "search", "/s?", "/s/")):
        return True
    query_terms = set(_norm(query).split())
    return bool(query_terms.intersection(_SHOPPING_QUERY_TERMS))


# ═══════════════════════════════════════════════════════════════
#  Field extraction & scoring
# ═══════════════════════════════════════════════════════════════

def _parse_product_fields(text: str) -> dict[str, Any]:
    """Extract structured product fields from element text."""
    fields: dict[str, Any] = {}
    price = _PRICE_RE.search(text or "")
    if price:
        fields["price"] = price.group(1).strip()
    was_price = _WAS_PRICE_RE.search(text or "")
    if was_price:
        fields["was_price"] = was_price.group(1).strip()
    discount = _DISCOUNT_RE.search(text or "")
    if discount:
        fields["discount_pct"] = int(discount.group(1))
    rating = _RATING_RE.search(text or "") or _STAR_RE.search(text or "")
    if rating:
        try:
            fields["rating"] = float(rating.group(1))
        except ValueError:
            pass
    reviews = _REVIEW_COUNT_RE.search(text or "")
    if reviews:
        fields["review_count"] = reviews.group(1).replace(",", "")
    brand = _BRAND_RE.search(text or "")
    if brand:
        fields["brand"] = brand.group(1).strip()
    return fields


def _score_product(snapshot_url: str, href: str, text: str) -> float:
    """Score an element as a product card (higher = more likely a product)."""
    score = 0.0
    parsed = _parse_product_fields(text)
    if parsed.get("price"):
        score += 45.0
    if parsed.get("rating"):
        score += 20.0
    if parsed.get("review_count"):
        score += 15.0
    if parsed.get("brand"):
        score += 10.0
    if parsed.get("was_price") or parsed.get("discount_pct"):
        score += 8.0
    # Product detail URL hint
    if any(hint in (href or "").lower() for hint in _PRODUCT_DETAIL_HINTS):
        score += 22.0
    # Known shopping domain
    if _domain(snapshot_url) in _SHOPPING_DOMAINS:
        score += 8.0
    # Reasonable text length (product titles tend to be descriptive)
    text_len = len((text or "").strip())
    if text_len >= 40:
        score += 6.0
    if text_len >= 100:
        score += 4.0
    return score


# ═══════════════════════════════════════════════════════════════
#  Main extractor
# ═══════════════════════════════════════════════════════════════

def extract_shopping_items(
    snapshot: Any,
    *,
    query: str = "",
    max_items: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract product-like cards from visible browser elements.

    Returns ``(items, metadata)`` — same contract as
    ``listing_extractor.extract_property_listing_items``.
    """
    if not snapshot or not getattr(snapshot, "elements", None):
        return [], {"extraction_strategy": "deterministic-product-cards", "confidence": 0.0}

    if not _looks_like_shopping_page(getattr(snapshot, "url", ""), query):
        return [], {"extraction_strategy": "deterministic-product-cards", "confidence": 0.0}

    grouped: dict[str, dict[str, Any]] = {}
    for el in snapshot.elements:
        if not getattr(el, "visible", True):
            continue
        href = _normalize_href(
            getattr(snapshot, "url", ""), getattr(el, "href", "")
        )
        text = " | ".join(
            part
            for part in (
                getattr(el, "text", "")
                or getattr(el, "aria_label", "")
                or getattr(el, "name", "")
                or "",
                getattr(el, "context_text", "") or "",
            )
            if part
        ).strip()
        if len(text) < 15:
            continue
        score = _score_product(getattr(snapshot, "url", ""), href, text)
        if score < 40.0:
            continue
        key = href or f"ref:{getattr(el, 'ref_id', '')}"
        existing = grouped.get(key)
        parsed = _parse_product_fields(text)
        item: dict[str, Any] = {
            "ref_id": str(getattr(el, "ref_id", "") or "").strip(),
            "label": (
                getattr(el, "text", "")
                or getattr(el, "primary_label", lambda: "")()
                or text
            ).strip()[:250],
            "context": (getattr(el, "context_text", "") or text).strip()[:300],
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

    items = sorted(
        grouped.values(),
        key=lambda x: float(x.get("score", 0.0)),
        reverse=True,
    )
    cap = max(1, min(int(max_items or 20), 30))
    for idx, item in enumerate(items[:cap], start=1):
        item["rank"] = idx

    final_items = items[:cap]
    confidence = (
        0.90 if len(final_items) >= 4
        else 0.80 if len(final_items) >= 2
        else 0.60 if final_items
        else 0.0
    )
    return final_items, {
        "extraction_strategy": "deterministic-product-cards",
        "source_domain": _domain(getattr(snapshot, "url", "")),
        "confidence": confidence,
        "item_count": len(final_items),
    }
