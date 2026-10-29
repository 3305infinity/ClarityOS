import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from browser.listing_extractor import extract_property_listing_items
from browser.models import ElementFingerprint, ElementRef, PageSnapshot


def test_extract_property_listing_items_from_zoopla_style_snapshot():
    snapshot = PageSnapshot(
        session_id="sess-1",
        tab_id="tab-1",
        url="https://www.zoopla.co.uk/to-rent/flats/egham/",
        title="Flats and apartments to rent in Egham - Zoopla",
        generation=1,
        elements=[
            ElementRef(
                ref_id="listing-1",
                generation=1,
                role="link",
                tag="a",
                text="£1,650 pcm (£380.77 pw) 1 bed 1 bath 1 reception Maxwell Mews, Egham, Surrey TW20",
                context_text="Guide price- available September 2025- A brand new split level apartment offer",
                href="https://www.zoopla.co.uk/to-rent/details/70873203/",
                action_types=["click"],
                fingerprint=ElementFingerprint(role="link", text="listing-1"),
            ),
            ElementRef(
                ref_id="listing-2",
                generation=1,
                role="link",
                tag="a",
                text="£1,750 pcm (£403.85 pw) 2 beds 2 baths 1 reception St. Judes Road, Englefield Green, Egham TW20",
                context_text="This spacious two double bedroom, first floor flat is situated",
                href="https://www.zoopla.co.uk/to-rent/details/71669421/",
                action_types=["click"],
                fingerprint=ElementFingerprint(role="link", text="listing-2"),
            ),
        ],
    )

    items, meta = extract_property_listing_items(snapshot, query="apartments for rent in Egham UK", max_items=10)

    assert len(items) == 2
    assert items[0]["price"].startswith("£")
    assert items[0]["href"].startswith("https://www.zoopla.co.uk/to-rent/details/")
    assert items[0]["bedrooms"] >= 1
    assert meta["extraction_strategy"] == "deterministic-property-cards"
    assert meta["source_domain"] == "zoopla.co.uk"


def test_extract_property_listing_items_returns_empty_for_non_listing_page():
    snapshot = PageSnapshot(
        session_id="sess-2",
        tab_id="tab-2",
        url="https://example.com/help",
        title="Help",
        generation=1,
        elements=[
            ElementRef(
                ref_id="help-link",
                generation=1,
                role="link",
                tag="a",
                text="Read the help guide",
                href="https://example.com/help/article",
                fingerprint=ElementFingerprint(role="link", text="help"),
            )
        ],
    )

    items, meta = extract_property_listing_items(snapshot, query="apartments for rent in Egham UK", max_items=10)

    assert items == []
    assert meta["confidence"] == 0.0
