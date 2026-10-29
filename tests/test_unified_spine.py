import os
import sys
import importlib.util

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))
import asyncio


def _load_module(module_name: str, relative_path: str):
    path = os.path.join(REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


route_policy = _load_module("route_policy", "backend/tools/route_policy.py")
search_policy = _load_module("search_policy", "backend/tools/search_policy.py")

from backend.browser.selector_ai import select_browser_candidate_with_flash
from backend.browser.models import ElementFingerprint, ElementRef, PageSnapshot
from backend.browser.store import browser_store

decide_web_route = route_policy.decide_web_route
choose_search_result = search_policy.choose_search_result


def test_route_policy_prefers_background_for_plain_research_queries():
    decision = decide_web_route(
        target_type="search_results",
        query="new antigravity update",
        url="",
        item_hint="",
        context={"background_mode": False, "browser_url": "https://www.google.com/search?q=antigravity"},
        runtime_state={"browser_state": {"connected": True, "url": "https://www.google.com/search?q=antigravity"}, "os_state": {}},
    )

    assert decision["route"] == "background_fetch"
    assert decision["policy"] == "deterministic-web-policy"


def test_route_policy_prefers_live_browser_when_chrome_is_frontmost():
    decision = decide_web_route(
        target_type="page_summary",
        query="recent housing market pricing trends 2024",
        url="",
        item_hint="",
        context={
            "background_mode": False,
            "browser_url": "https://www.google.com/search?q=housing",
            "active_app": "Google Chrome",
        },
        runtime_state={
            "browser_state": {"connected": True, "url": "https://www.google.com/search?q=housing"},
            "os_state": {"active_app": "Google Chrome"},
        },
    )

    assert decision["route"] == "browser_aci"


def test_search_policy_prefers_authoritative_changelog_result():
    chosen, meta = choose_search_result(
        [
            {"label": "Antigravity changelog", "href": "https://antigravity.google/changelog", "context": "Official product updates"},
            {"label": "Random discussion thread", "href": "https://reddit.com/r/example/comments/123", "context": "User reactions"},
        ],
        query="new antigravity update",
        target_type="page_summary",
    )

    assert chosen is not None
    assert chosen["href"] == "https://antigravity.google/changelog"
    assert meta["search_follow_strategy"] == "deterministic-ranking"


def test_browser_selector_uses_deterministic_resolver_without_llm():
    browser_store.reset()
    browser_store.upsert_snapshot(
        PageSnapshot(
            session_id="selector-session",
            tab_id="tab-1",
            url="https://example.com",
            title="Example",
            generation=1,
            elements=[
                ElementRef(
                    ref_id="primary",
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
                    ref_id="secondary",
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
    )

    selection, error = asyncio.run(
        select_browser_candidate_with_flash(
            "Continue",
            "click",
            session_id="selector-session",
        )
    )

    assert error == ""
    assert selection["ref_id"] == "primary"
    assert selection["model"] == "deterministic-resolver"
