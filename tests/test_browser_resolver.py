"""
Test browser resolver and stable ref contract basics.
"""

import asyncio
import os
import sys

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)
sys.path.insert(0, os.path.join(repo_root, "backend"))

from backend.browser.models import ElementRef, ElementFingerprint, PageSnapshot
from backend.browser.resolver import BrowserResolver
from backend.browser.selector_ai import select_browser_candidate_with_flash
from backend.browser.store import browser_store


def test_browser_resolver_prefers_exact_button_match():
    resolver = BrowserResolver()
    elements = [
        ElementRef(
            ref_id="btn_continue_billing",
            generation=1,
            role="button",
            tag="button",
            text="Continue",
            context_text="Billing section",
            action_types=["click"],
            fingerprint=ElementFingerprint(
                role="button",
                text="Continue",
                ancestor_labels=["Billing section"],
            ),
        ),
        ElementRef(
            ref_id="btn_continue_shipping",
            generation=1,
            role="button",
            tag="button",
            text="Continue",
            context_text="Shipping section",
            action_types=["click"],
            fingerprint=ElementFingerprint(
                role="button",
                text="Continue",
                ancestor_labels=["Shipping section"],
            ),
        ),
    ]

    matches = resolver.describe_candidates("Continue billing", elements, action="click", limit=2)
    assert matches[0]["ref_id"] == "btn_continue_billing"
    assert matches[0]["score"] >= matches[1]["score"]


def test_browser_store_upserts_snapshot_and_refs():
    snapshot = PageSnapshot(
        session_id="session-1",
        tab_id="tab-1",
        url="https://example.com",
        title="Example",
        generation=3,
        elements=[
            ElementRef(
                ref_id="email_input",
                generation=3,
                role="textbox",
                tag="input",
                placeholder="Email",
                action_types=["type"],
                fingerprint=ElementFingerprint(role="textbox", placeholder="Email"),
            )
        ]
    )

    browser_store.upsert_snapshot(snapshot)

    current = browser_store.get_snapshot("session-1")
    assert current is not None
    assert current.generation == 3
    assert browser_store.get_element("email_input", "session-1") is not None


def test_browser_resolver_prefers_search_input_over_search_button_for_click():
    resolver = BrowserResolver()
    elements = [
        ElementRef(
            ref_id="search_button",
            generation=1,
            role="button",
            tag="button",
            text="Search",
            action_types=["click"],
            fingerprint=ElementFingerprint(role="button", text="Search"),
        ),
        ElementRef(
            ref_id="search_input",
            generation=1,
            role="combobox",
            tag="input",
            name="search_query",
            placeholder="Search",
            action_types=["click", "type", "select"],
            fingerprint=ElementFingerprint(role="combobox", name="search_query", placeholder="Search"),
        ),
    ]

    matches = resolver.describe_candidates("main search box", elements, action="click", limit=2)
    assert matches[0]["ref_id"] == "search_input"
    assert matches[0]["score"] > matches[1]["score"]


def test_browser_resolver_prefers_search_button_when_query_explicitly_requests_button():
    resolver = BrowserResolver()
    elements = [
        ElementRef(
            ref_id="search_button",
            generation=1,
            role="button",
            tag="button",
            text="Search",
            action_types=["click"],
            fingerprint=ElementFingerprint(role="button", text="Search"),
        ),
        ElementRef(
            ref_id="search_input",
            generation=1,
            role="combobox",
            tag="input",
            name="search_query",
            placeholder="Search",
            action_types=["click", "type", "select"],
            fingerprint=ElementFingerprint(role="combobox", name="search_query", placeholder="Search"),
        ),
    ]

    matches = resolver.describe_candidates("search button", elements, action="click", limit=2)
    assert matches[0]["ref_id"] == "search_button"


def test_browser_selector_uses_flash_when_available(monkeypatch):
    browser_store.reset()
    snapshot = PageSnapshot(
        session_id="flash-session",
        tab_id="tab-1",
        url="https://www.youtube.com/results?search_query=japanese",
        title="YouTube",
        generation=1,
        elements=[
            ElementRef(
                ref_id="video_1",
                generation=1,
                role="link",
                tag="a",
                text="Japanese Listening Practice For Beginners",
                href="/watch?v=abc123",
                action_types=["click"],
                visible=True,
                enabled=True,
                fingerprint=ElementFingerprint(
                    role="link",
                    text="Japanese Listening Practice For Beginners",
                    href="/watch?v=abc123",
                ),
            ),
            ElementRef(
                ref_id="video_2",
                generation=1,
                role="link",
                tag="a",
                text="JLPT N5 Shadowing Exercise",
                href="/watch?v=def456",
                action_types=["click"],
                visible=True,
                enabled=True,
                fingerprint=ElementFingerprint(
                    role="link",
                    text="JLPT N5 Shadowing Exercise",
                    href="/watch?v=def456",
                ),
            ),
        ],
    )
    browser_store.upsert_snapshot(snapshot)

    selection, error = asyncio.run(
        select_browser_candidate_with_flash(
            "video thumbnail or title",
            "click",
            session_id="flash-session",
            limit=5,
        )
    )

    assert error == ""
    assert selection["ref_id"] == "video_1"
    assert selection["model"] == "deterministic-resolver"
    assert selection["confidence"] > 0.6


def test_browser_selector_marks_ambiguous_matches_as_degraded():
    browser_store.reset()
    snapshot = PageSnapshot(
        session_id="fallback-session",
        tab_id="tab-1",
        url="https://example.com",
        title="Example",
        generation=1,
        elements=[
            ElementRef(
                ref_id="primary_button",
                generation=1,
                role="button",
                tag="button",
                text="Continue",
                action_types=["click"],
                visible=True,
                enabled=True,
                fingerprint=ElementFingerprint(role="button", text="Continue"),
            ),
            ElementRef(
                ref_id="secondary_button",
                generation=1,
                role="button",
                tag="button",
                text="Continue later",
                action_types=["click"],
                visible=True,
                enabled=True,
                fingerprint=ElementFingerprint(role="button", text="Continue later"),
            ),
        ],
    )
    browser_store.upsert_snapshot(snapshot)

    selection, error = asyncio.run(
        select_browser_candidate_with_flash(
            "Continue",
            "click",
            session_id="fallback-session",
            limit=5,
        )
    )

    assert error == ""
    assert selection["ref_id"] == "primary_button"
    assert selection["model"] == "deterministic-resolver"
    assert selection["degraded_mode"] is False
    assert selection["degraded_reason"] == ""


def test_browser_resolver_boosts_in_viewport_elements():
    """Elements in the viewport should score higher than identical off-screen ones."""
    resolver = BrowserResolver()
    visible_element = ElementRef(
        ref_id="visible_btn",
        generation=1,
        role="button",
        tag="button",
        text="Submit",
        in_viewport=True,
        action_types=["click"],
        fingerprint=ElementFingerprint(role="button", text="Submit"),
    )
    offscreen_element = ElementRef(
        ref_id="offscreen_btn",
        generation=1,
        role="button",
        tag="button",
        text="Submit",
        in_viewport=False,
        action_types=["click"],
        fingerprint=ElementFingerprint(role="button", text="Submit"),
    )
    ranked = resolver.describe_candidates("Submit", [offscreen_element, visible_element], action="click")
    # The in-viewport element should rank first
    assert ranked[0]["ref_id"] == "visible_btn"
    assert ranked[0]["score"] > ranked[1]["score"]
