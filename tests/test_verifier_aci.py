"""
Unit tests for ACI verifier guards.
"""
import sys
import os
import json
import asyncio
from typing import Optional

backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from agent.verifier import ToolVerifier


def _verify(tool_name: str, tool_result: str, tool_args: Optional[dict] = None):
    verifier = ToolVerifier()

    async def run():
        return await verifier.verify(
            tool_name=tool_name,
            tool_args=tool_args or {},
            tool_result=tool_result,
            success_criteria="",
        )

    return asyncio.run(run())


def _verify_with_visual(tool_name: str, tool_result: str, tool_args: Optional[dict] = None, visual_summary: str = ""):
    verifier = ToolVerifier()

    async def _visual():
        return visual_summary

    async def run():
        return await verifier.verify_with_visual(
            tool_name=tool_name,
            tool_args=tool_args or {},
            tool_result=tool_result,
            success_criteria="",
            get_visual_state=_visual,
        )

    return asyncio.run(run())


def test_verify_read_page_content_empty_fails():
    result = json.dumps(
        {
            "url": "https://example.com",
            "title": "Example",
            "paragraph_count": 0,
            "content_length": 0,
            "content": "",
        }
    )
    verification = _verify("read_page_content", result)
    assert verification.success is False


def test_verify_extract_structured_data_empty_fails():
    result = json.dumps(
        {
            "url": "https://example.com",
            "item_type": "results",
            "items": [],
            "item_count": 0,
        }
    )
    verification = _verify("extract_structured_data", result)
    assert verification.success is False


def test_verify_find_and_act_ok_false_fails():
    result = json.dumps(
        {
            "ok": False,
            "message": "Could not find element",
            "error_code": "element_not_found",
        }
    )
    verification = _verify(
        "find_and_act",
        result,
        {"target": "Rightmove", "action": "click"},
    )
    assert verification.success is False


def test_verify_get_page_summary_zero_elements_fails():
    result = json.dumps(
        {
            "url": "https://example.com",
            "title": "Example",
            "page_type": "unknown",
            "total_elements": 0,
        }
    )
    verification = _verify("get_page_summary", result)
    assert verification.success is False


def test_verify_web_scrape_ok_payload_succeeds():
    result = json.dumps(
        {
            "ok": True,
            "url": "https://example.com",
            "title": "Example",
            "content": "x" * 160,
            "content_length": 160,
            "links": [],
        }
    )
    verification = _verify("web_scrape", result, {"url": "https://example.com"})
    assert verification.success is True


def test_verify_get_web_information_ignores_degraded_route_metadata_on_success():
    result = json.dumps(
        {
            "ok": True,
            "target_type": "page_summary",
            "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
            "title": "UK Housing Review",
            "summary": "The review provides annual analysis of the UK housing sector across supply, affordability, and tenure.",
            "headings": [{"text": "Annual housing analysis", "tag": "h2", "ref_id": ""}],
            "page_type": "report",
            "content": "The review provides annual analysis of the UK housing sector across supply, affordability, and tenure.",
            "content_length": 103,
            "route": "background_fetch",
            "route_decision_degraded": True,
            "route_decision_reason": "Flash browser interpreter timed out after 10.0s.",
            "route_decision_error_code": "flash_timeout",
        }
    )
    verification = _verify(
        "get_web_information",
        result,
        {"target_type": "page_summary", "url": "https://www.cih.org/knowledge-hub/uk-housing-review/"},
    )
    assert verification.success is True


def test_verify_click_ui_not_found_fails():
    verification = _verify(
        "click_ui",
        "No UI element matching 'Kris' found in the accessibility tree. The element may not be visible, or the app may not expose it via Accessibility. Try read_screen as a fallback to visually locate it.",
        {"description": "Kris", "app_name": "WhatsApp"},
    )
    assert verification.success is False


def test_verify_type_in_field_not_found_fails():
    verification = _verify(
        "type_in_field",
        "No text field matching 'Search' found. Try read_screen to visually locate the field.",
        {"field_description": "Search", "text": "Kris", "app_name": "WhatsApp"},
    )
    assert verification.success is False


def test_verify_get_ui_tree_timeout_fails():
    verification = _verify(
        "get_ui_tree",
        "ERROR: Timed out getting UI tree",
        {"app_name": "WhatsApp"},
    )
    assert verification.success is False


def test_verify_gdocs_create_partial_success_requests_same_doc_repair():
    verification = _verify(
        "gdocs_create",
        json.dumps(
            {
                "ok": False,
                "url": "https://docs.google.com/document/d/abc123/edit",
                "note": "Google Doc opened, but title or body was not applied reliably.",
                "error_code": "gdocs_apply_failed",
                "title_applied": True,
                "body_applied": False,
                "repairable": True,
            }
        ),
        {"title": "Egham Apartment Research"},
    )
    assert verification.success is False
    assert verification.should_retry is True
    assert "same Google Doc" in (verification.suggested_fix or "")


def test_verify_with_visual_keeps_successful_gdocs_create_without_visual_override():
    verification = _verify_with_visual(
        "gdocs_create",
        json.dumps(
            {
                "ok": True,
                "url": "https://docs.google.com/document/d/abc123/edit",
                "doc_id": "abc123",
                "note": "Opened a new Google Doc and applied the requested title/content.",
            }
        ),
        {"title": "Fascinating Facts About Octopuses"},
        visual_summary="App: Electron\nTitle: Moonwalk\nProceed button visible",
    )
    assert verification.success is True
    assert "Google Doc created" in verification.message


def test_verify_gdocs_append_success_requires_real_append_signal():
    verification = _verify(
        "gdocs_append",
        json.dumps(
            {
                "ok": True,
                "doc_id": "abc123",
                "url": "https://docs.google.com/document/d/abc123/edit",
                "appended_chars": 3176,
                "method": "keyboard_paste_html",
            }
        ),
        {"doc_url_or_id": "abc123", "text": "Pizza history"},
    )
    assert verification.success is True
    assert "Google Doc updated" in verification.message
