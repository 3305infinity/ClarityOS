"""
Focused reliability checks for the Moonwalk recovery plan.

Run directly with:
  python3 tests/test_reliability_recovery.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from agent.planner import ExecutionStep, Milestone, MilestonePlan, StepStatus
from agent.task_planner import TaskPlanner
from agent.core_v2 import MoonwalkAgentV2, PendingPlanState
from agent.memory import WorkingMemory
from agent.template_registry import TemplateRegistry
from agent.verifier import VerificationResult
from agent.perception import ContextSnapshot
from agent.world_state import WorldState
from browser.bridge import browser_bridge
from browser.models import ActionResult, PageSnapshot, ViewportMeta
from browser.store import browser_store
from providers.base import LLMResponse
from runtime_state import runtime_state_store
from tools import registry as tool_registry
from tools.contracts import error_envelope
from tools.browser_tools import browser_click_ref, browser_describe_ref, browser_refresh_refs
from tools.file_tools import read_file, write_file
from tools.mac_tools import await_reply, send_response
import tools.selector as selector_module
from tools.selector import ToolSelector, get_web_information, set_tool_gateway_context


def _milestone_plan(summary: str, *tool_groups: list[str]) -> MilestonePlan:
    milestones = [
        Milestone(
            id=idx,
            goal=f"Milestone {idx}",
            success_signal="done",
            hint_tools=list(tool_names),
        )
        for idx, tool_names in enumerate(tool_groups, start=1)
    ]
    return MilestonePlan(task_summary=summary, milestones=milestones)


async def test_response_contract_aliases():
    payload = await send_response(response_text="hello")
    assert payload.startswith("RESPONSE:")
    data = json.loads(payload[len("RESPONSE:"):])
    assert data["message"] == "hello"

    payload = await await_reply(prompt="what next?")
    assert payload.startswith("AWAIT:")
    data = json.loads(payload[len("AWAIT:"):])
    assert data["message"] == "what next?"


async def test_response_contract_cards_payloads():
    payload = await send_response(
        message="Here are some example products for you to check out.",
        modal="cards",
        title="Recommended products",
        subtitle="2 results",
        cards=[
            {"name": "Wireless Headphones", "price": "$299.00", "description": "Noise canceling"},
            {"title": "Fitness Watch", "price": "$199.00", "description": "Sleep and workout tracking"},
        ],
    )
    assert payload.startswith("RESPONSE:")
    data = json.loads(payload[len("RESPONSE:"):])
    assert data["modal"] == "cards"
    assert data["title"] == "Recommended products"
    assert data["subtitle"] == "2 results"
    assert len(data["cards"]) == 2
    assert data["cards"][0]["name"] == "Wireless Headphones"
    assert data["cards"][1]["title"] == "Fitness Watch"

    legacy = await send_response(
        message="Legacy products payload",
        modal="products",
        products=[{"name": "Legacy Product", "price": "$19.99"}],
    )
    legacy_data = json.loads(legacy[len("RESPONSE:"):])
    assert legacy_data["modal"] == "products"
    assert legacy_data["cards"][0]["name"] == "Legacy Product"
    assert legacy_data["products"][0]["name"] == "Legacy Product"

    await_payload = await await_reply(
        message="Which product do you want?",
        modal="cards",
        cards=[{"name": "Option A", "price": "$9.99"}],
    )
    await_data = json.loads(await_payload[len("AWAIT:"):])
    assert await_data["modal"] == "cards"
    assert await_data["cards"][0]["name"] == "Option A"


def test_error_envelope_preserves_legacy_fields():
    payload = error_envelope(
        "browser.no_snapshot",
        "No active browser snapshot is available.",
        source="tool.browser",
        details={"session_id": "abc"},
        flatten_details=True,
    )

    assert payload["ok"] is False
    assert payload["error_code"] == "browser.no_snapshot"
    assert payload["message"] == "No active browser snapshot is available."
    assert payload["error"]["code"] == "browser.no_snapshot"
    assert payload["error"]["source"] == "tool.browser"
    assert payload["session_id"] == "abc"


def test_runtime_state_prefers_bridge_browser_truth():
    browser_store.reset()
    browser_bridge.reset()
    runtime_state_store.reset()
    runtime_state_store.start_request(request_id="req-1", query="test")
    runtime_state_store.update_os_state(
        active_app="Google Chrome",
        browser_url="https://fallback.example.com",
        provenance="applescript_fallback",
        degraded=True,
    )

    browser_bridge.register_connection("bridge-session", "moonwalk-browser-bridge")
    browser_store.upsert_snapshot(
        PageSnapshot(
            session_id="bridge-session",
            tab_id="tab-1",
            url="https://bridge.example.com/article",
            title="Bridge Title",
            generation=2,
            viewport=ViewportMeta(width=1280, height=800),
        )
    )
    browser_bridge.register_snapshot(browser_store.get_snapshot("bridge-session"))

    snapshot = runtime_state_store.snapshot()
    assert snapshot.browser_state.connected is True
    assert snapshot.browser_state.url == "https://bridge.example.com/article"
    assert snapshot.os_state.browser_url == "https://bridge.example.com/article"
    assert snapshot.os_state.browser_url_provenance == "browser_bridge"
    assert snapshot.os_state.browser_url_degraded is False


async def test_file_tools_reliability():
    with tempfile.TemporaryDirectory() as tmp_dir:
        prev_cwd = os.getcwd()
        os.chdir(tmp_dir)
        try:
            result = await write_file("relative.txt", "alpha\nbeta\ngamma\ndelta\n")
            assert "relative.txt" in result
            assert os.path.isfile(os.path.join(tmp_dir, "relative.txt"))

            page = await read_file("relative.txt", offset=6, max_chars=9)
            assert "offset 6" in page
            assert "beta\ngamm" in page

            numbered = await read_file("relative.txt", offset=0, max_chars=12, include_line_numbers=True)
            assert "1:" in numbered
            assert "2:" in numbered
        finally:
            os.chdir(prev_cwd)


async def _type_in_field_falls_back_to_unfiltered_inputs_async():
    import tools.mac_tools as mac_tools

    original_get_cached_ui_tree = mac_tools._get_cached_ui_tree
    original_click_element = mac_tools.click_element
    original_type_text = mac_tools.type_text
    original_activate_target_app = mac_tools._activate_target_app
    try:
        async def _fake_get_cached_ui_tree(app_name: str = "", search_term: str = ""):
            if search_term:
                return ([], "")
            return (
                [
                    {"role": "AXSearchField", "name": "", "x": 10, "y": 20, "w": 120, "h": 28, "cx": 70, "cy": 34},
                ],
                "",
            )

        async def _fake_click_element(*args, **kwargs):
            return "clicked"

        async def _fake_type_text(text: str):
            return f"Typed {len(text)} characters into the active field."

        async def _fake_activate_target_app(app_name: str):
            return None

        mac_tools._get_cached_ui_tree = _fake_get_cached_ui_tree
        mac_tools.click_element = _fake_click_element
        mac_tools.type_text = _fake_type_text
        mac_tools._activate_target_app = _fake_activate_target_app

        result = await mac_tools.type_in_field(
            field_description="Search",
            text="Kris",
            app_name="WhatsApp",
        )
        assert "then typed 4 chars" in result
    finally:
        mac_tools._get_cached_ui_tree = original_get_cached_ui_tree
        mac_tools.click_element = original_click_element
        mac_tools.type_text = original_type_text
        mac_tools._activate_target_app = original_activate_target_app


def test_type_in_field_falls_back_to_unfiltered_inputs():
    asyncio.run(_type_in_field_falls_back_to_unfiltered_inputs_async())


def test_selector_coverage():
    selector = ToolSelector()

    file_tools = selector.select("list this directory and replace TODO in app.py")
    assert "list_directory" in file_tools
    assert "replace_in_file" in file_tools

    browser_tools = selector.select(
        "fill this form and choose the country option",
        context_app="Google Chrome",
        context_url="https://example.com/form",
    )
    assert "browser_click_ref" in browser_tools
    assert "browser_type_ref" in browser_tools
    assert "browser_select_ref" in browser_tools

    research_tools = selector.select(
        "find me the best housing in the UK and write a report in a Google document",
        context_app="Google Chrome",
        context_url="https://www.google.com/search?q=uk+housing",
    )
    assert "get_web_information" in research_tools
    assert "browser_read_page" not in research_tools
    assert "gdocs_create" in research_tools


def test_selector_uses_clipboard_source_for_overview_request():
    selector = ToolSelector()

    tools = selector.select(
        "message kris with an overview of what i need to do during the encode demo",
        context_app="WhatsApp",
        clipboard_content="https://lu.ma/encode-demo",
    )

    assert "get_web_information" in tools
    assert "open_url" in tools


def test_selector_messaging_fast_path_ignores_research_context():
    selector = ToolSelector()

    tools = selector.select(
        "message kris about the information for the upcoming meeting",
        context_app="WhatsApp",
        conversation_history="research the housing market and write a report in a Google document",
        clipboard_content="",
        selected_text="",
        intent_action="communicate",
        intent_target_type="content",
    )

    assert tools == [
        "send_response",
        "await_reply",
        "open_app",
        "click_ui",
        "type_in_field",
        "type_text",
        "press_key",
        "run_shortcut",
        "get_ui_tree",
    ]


def test_milestone_hint_equivalents_keep_typing_tools_for_ui_milestones():
    allowed = selector_module.resolve_milestone_allowed_tools(
        ["click_ui", "press_key"],
        {
            "send_response",
            "await_reply",
            "open_app",
            "click_ui",
            "type_in_field",
            "type_text",
            "press_key",
            "run_shortcut",
        },
    )

    assert allowed is not None
    assert "type_in_field" in allowed
    assert "type_text" in allowed
    assert "open_app" in allowed


def test_selector_media_fast_path_ignores_research_context():
    selector = ToolSelector()

    tools = selector.select(
        "open a funny video",
        context_app="Electron",
        conversation_history="research the housing market and write a report in a Google document",
        clipboard_content="https://lu.ma/encode-demo",
        selected_text="summary tasks responsibilities",
        intent_action="open",
        intent_target_type="unknown",
    )

    assert tools == ["send_response", "await_reply", "play_media", "open_url"]


async def _planner_media_shortcut_for_direct_open_requests_async():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)

    generic_plan = await planner.create_plan(
        "open a funny video",
        WorldState(active_app="Electron"),
        available_tools=["send_response", "await_reply", "play_media", "open_url"],
        conversation_history=[],
    )
    assert generic_plan.source == "milestone_media_shortcut"
    assert len(generic_plan.milestones) == 1
    assert generic_plan.milestones[0].hint_tools == ["play_media"]

    explicit_plan = await planner.create_plan(
        "open https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        WorldState(active_app="Electron"),
        available_tools=["send_response", "await_reply", "play_media", "open_url"],
        conversation_history=[],
    )
    assert explicit_plan.source == "milestone_media_shortcut"
    assert explicit_plan.milestones[0].hint_tools == ["open_url"]


def test_planner_media_shortcut_for_direct_open_requests():
    asyncio.run(_planner_media_shortcut_for_direct_open_requests_async())


async def _planner_repeat_message_shortcut_async():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)

    repeat_plan = await planner.create_plan(
        "send it again please",
        WorldState(active_app="WhatsApp"),
        available_tools=["send_response", "await_reply", "type_in_field", "type_text", "press_key", "run_shortcut"],
        conversation_history=[],
    )
    assert repeat_plan.source == "milestone_repeat_message_shortcut"
    assert len(repeat_plan.milestones) == 1
    assert repeat_plan.milestones[0].hint_tools == ["type_in_field", "type_text", "press_key"]


def test_planner_repeat_message_shortcut():
    asyncio.run(_planner_repeat_message_shortcut_async())


def test_working_memory_tracks_last_typed_text():
    memory = WorkingMemory()
    memory.log_action("type_text", {"text": "Hello Kris"}, "Typed 10 characters into the active field.", success=True)
    assert memory.get_last_typed_text() == "Hello Kris"


def test_selector_llm_surface_abstracts_web_information():
    selector = ToolSelector(tool_registry)
    decls = selector.get_llm_tool_declarations([
        "open_url",
        "web_search",
        "browser_read_page",
        "read_page_content",
        "extract_structured_data",
        "get_page_summary",
        "gdocs_create",
    ])
    names = [decl["name"] for decl in decls]

    assert "get_web_information" in names
    assert "open_url" in names
    assert "gdocs_create" in names
    assert "web_search" not in names
    assert "browser_read_page" not in names
    assert "read_page_content" not in names
    assert "extract_structured_data" not in names
    assert "get_page_summary" not in names


def test_plan_tool_contract_requires_supported_hints():
    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    plan = MilestonePlan(
        task_summary="Find encode demo tasks",
        milestones=[
            Milestone(
                id=1,
                goal="Read the demo source",
                success_signal="Source content is extracted",
                hint_tools=["run_shell", "open_url"],
            )
        ],
    )

    enforced = agent._enforce_plan_tool_contract(
        plan,
        [
            {"name": "open_url", "parameters": {"type": "object", "properties": {}}},
            {"name": "await_reply", "parameters": {"type": "object", "properties": {}}},
            {"name": "send_response", "parameters": {"type": "object", "properties": {}}},
        ],
    )

    assert enforced.needs_clarification is True
    assert "run_shell" in enforced.clarification_prompt


def test_research_document_template_bypass():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)
    world_state = WorldState(active_app="Google Chrome", browser_url="https://www.google.com")
    intent = planner.intent_parser.parse(
        "find me the best housing in the UK and tell me in a Google document why it is best"
    )
    plan = planner._try_template(intent, world_state)
    assert plan is None


async def test_browser_error_payloads():
    invalid_ref = json.loads(await browser_click_ref("btn_submit"))
    assert invalid_ref["ok"] is False
    assert invalid_ref["error_code"] == "invalid_ref_format"

    missing_ref = json.loads(await browser_click_ref("mw_9999"))
    assert missing_ref["ok"] is False
    assert missing_ref["error_code"] == "unknown_ref"

    missing_desc = json.loads(await browser_describe_ref("mw_9999"))
    assert missing_desc["ok"] is False
    assert missing_desc["error_code"] == "unknown_ref"


async def test_get_web_information_background_search_route():
    set_tool_gateway_context(active_app="Terminal", browser_url="", background_mode=True)
    original_execute = tool_registry.execute

    async def _tracking_execute(name, args):
        if name == "web_scrape":
            return json.dumps(
                {
                    "ok": True,
                    "url": "https://html.duckduckgo.com/html/?q=uk+housing",
                    "title": "DuckDuckGo",
                    "content": "UK housing search results",
                    "content_length": 120,
                    "links": [
                        {"label": "https://html.duckduckgo.com/html/", "url": "https://html.duckduckgo.com/html/?q=uk+housing"},
                        {
                            "label": "Housing overview",
                            "url": "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fhousing",
                        },
                        {"label": "Rental data", "url": "https://example.com/rentals"},
                    ],
                    "link_count": 3,
                }
            )
        return await original_execute(name, args)

    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(await get_web_information(query="uk housing", target_type="search_results"))
        assert payload["ok"] is True
        assert payload["route"] == "background_fetch"
        assert payload["item_count"] == 2
        assert payload["items"][0]["href"] == "https://example.com/housing"
        assert "html.duckduckgo.com/html" not in payload["content"]
    finally:
        tool_registry.execute = original_execute


async def test_get_web_information_flash_browser_route_for_search():
    set_tool_gateway_context(active_app="Electron", browser_url="", background_mode=False)
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash

    async def _fake_route_decider(**kwargs):
        return {
            "route": "browser_aci",
            "reason": "Use live browser search flow so the agent can inspect and open sources interactively.",
            "confidence": 0.93,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    calls = []

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "web_search":
            return "Opened web search for 'uk housing'"
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        if name == "extract_structured_data":
            return json.dumps(
                {
                    "url": "https://www.google.com/search?q=uk+housing",
                    "title": "Google",
                    "item_type": "results",
                    "items": [{"label": "Housing overview", "href": "https://example.com/housing"}],
                    "item_count": 1,
                }
            )
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _fake_route_decider
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(await get_web_information(query="uk housing", target_type="search_results"))
        assert payload["ok"] is True
        assert payload["route"] == "browser_aci"
        assert payload["route_decision_model"] == "gemini-3-flash-preview"
        assert payload["route_decision_degraded"] is False
        assert payload["item_count"] == 1
        assert any(name == "web_search" for name, _ in calls)
        assert any(name == "extract_structured_data" for name, _ in calls)
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        tool_registry.execute = original_execute


async def test_get_web_information_live_bridge_failure_stays_on_browser_route():
    set_tool_gateway_context(
        active_app="Electron",
        browser_url="",
        background_mode=False,
        browser_bridge_connected=True,
        browser_has_snapshot=True,
        browser_session_id="browser-session",
    )
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash

    async def _fake_route_decider(**kwargs):
        return {
            "route": "browser_aci",
            "reason": "Live browser bridge is available.",
            "confidence": 0.95,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    calls = []

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "web_search":
            return "Opened web search for 'funny video'"
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        if name == "extract_structured_data":
            return json.dumps(
                {
                    "ok": False,
                    "message": "No structured items matched on the current page.",
                    "error_code": "empty_items",
                    "url": "https://www.google.com/search?q=funny+video",
                    "title": "Google",
                }
            )
        if name == "browser_scroll":
            return json.dumps({"ok": True, "at_bottom": True, "message": "scrolled"})
        if name == "web_scrape":
            raise AssertionError("Live browser failures should not degrade to background fetch fallback")
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _fake_route_decider
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(await get_web_information(query="funny video", target_type="search_results"))
        assert payload["ok"] is False
        assert payload["route"] == "browser_aci"
        assert payload["error_code"] == "browser_search_no_results"
        assert not any(name == "web_scrape" for name, _ in calls)
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        tool_registry.execute = original_execute


async def test_browser_refresh_refs_bootstraps_initial_snapshot():
    original_is_connected = browser_bridge.is_connected
    original_connected_session_id = browser_bridge.connected_session_id
    original_queue_action = browser_bridge.queue_action
    browser_store.reset()
    queued_actions = []

    def _fake_queue_action(request):
        queued_actions.append(request)
        return ActionResult(
            ok=True,
            message="queued",
            action=request.action,
            session_id=request.session_id,
        )

    browser_bridge.is_connected = lambda: True
    browser_bridge.connected_session_id = lambda: "bootstrap-session"
    browser_bridge.queue_action = _fake_queue_action
    try:
        refresh_task = asyncio.create_task(browser_refresh_refs(timeout=0.3))
        await asyncio.sleep(0.05)
        browser_store.upsert_snapshot(
            PageSnapshot(
                session_id="bootstrap-session",
                tab_id="tab-1",
                url="https://www.google.com/search?q=funny+video",
                title="Google",
                generation=1,
                viewport=ViewportMeta(width=1280, height=800),
            )
        )
        payload = json.loads(await refresh_task)
        assert payload["ok"] is True
        assert payload["session_id"] == "bootstrap-session"
        assert "bootstrapped" in payload["message"].lower()
        assert queued_actions and queued_actions[0].action == "refresh_snapshot"
    finally:
        browser_bridge.is_connected = original_is_connected
        browser_bridge.connected_session_id = original_connected_session_id
        browser_bridge.queue_action = original_queue_action
        browser_store.reset()


async def test_get_web_information_query_page_summary_runs_search_follow_read_loop():
    set_tool_gateway_context(active_app="Google Chrome", browser_url="https://www.google.com", background_mode=False)
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash
    original_choice = selector_module.choose_search_result_with_flash

    async def _fake_route_decider(**kwargs):
        return {
            "route": "browser_aci",
            "reason": "Use live browser research flow.",
            "confidence": 0.96,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    async def _fake_choice(**kwargs):
        return {
            "selected_href": "https://www.cih.org/knowledge-hub/uk-housing-review/",
            "selected_ref_id": "mw_result_1",
            "selected_label": "UK Housing Review - CIH",
            "reason": "This is an authoritative sector analysis directly relevant to the query.",
            "confidence": 0.91,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    calls = []

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "web_search":
            return "Opened web search for 'uk housing review'"
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        if name == "extract_structured_data":
            if args.get("item_type") == "search_result":
                return json.dumps(
                    {
                        "ok": True,
                        "url": "https://www.google.com/search?q=uk+housing+review",
                        "title": "Google",
                        "items": [
                            {
                                "ref_id": "mw_result_1",
                                "label": "UK Housing Review - CIH",
                                "href": "https://www.cih.org/knowledge-hub/uk-housing-review/",
                                "context": "Annual analysis of the UK housing sector.",
                                "rank": 1,
                            }
                        ],
                        "item_count": 1,
                    }
                )
            raise AssertionError(f"Unexpected structured extraction args: {args}")
        if name == "browser_click_ref":
            return json.dumps({"ok": True, "message": "clicked", "ref_id": "mw_result_1"})
        if name == "get_page_summary":
            return json.dumps(
                {
                    "ok": True,
                    "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
                    "title": "UK Housing Review - the housing sector's annual analysis",
                    "summary": "The page provides annual analysis of UK housing supply, affordability, and tenure trends.",
                    "headings": [{"text": "Annual housing analysis", "tag": "h2", "ref_id": ""}],
                    "page_type": "report",
                    "content": "The page provides annual analysis of UK housing supply, affordability, and tenure trends.",
                    "content_length": 94,
                    "summary_strategy": "readability",
                    "total_elements": 6,
                    "item_count": 1,
                }
            )
        if name == "open_url":
            raise AssertionError("Query-driven browser research should follow the visible result instead of opening the URL directly")
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _fake_route_decider
    selector_module.choose_search_result_with_flash = _fake_choice
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(
            await get_web_information(
                query="uk housing review",
                target_type="page_summary",
            )
        )
        assert payload["ok"] is True
        assert payload["route"] == "browser_aci"
        assert payload["selected_source_url"] == "https://www.cih.org/knowledge-hub/uk-housing-review/"
        assert payload["search_follow_strategy"] == "deterministic-ranking"
        assert payload["summary"].startswith("The page provides annual analysis")
        assert payload["content"].startswith("The page provides annual analysis")
        assert payload["content_length"] == 94
        assert payload["summary_strategy"] == "readability"
        call_names = [name for name, _ in calls]
        assert "web_search" in call_names
        assert "extract_structured_data" in call_names
        assert "browser_click_ref" in call_names
        assert "get_page_summary" in call_names
        assert call_names.index("browser_click_ref") > call_names.index("extract_structured_data")
        assert call_names.index("get_page_summary") > call_names.index("browser_click_ref")
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        selector_module.choose_search_result_with_flash = original_choice
        tool_registry.execute = original_execute


async def test_get_web_information_browser_search_scrolls_before_fallback():
    set_tool_gateway_context(active_app="Google Chrome", browser_url="https://www.google.com", background_mode=False)
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash

    async def _fake_route_decider(**kwargs):
        return {
            "route": "browser_aci",
            "reason": "Use live browser search flow so the agent can inspect and open sources interactively.",
            "confidence": 0.94,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    calls = []
    extract_calls = 0

    async def _tracking_execute(name, args):
        nonlocal extract_calls
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "web_search":
            return "Opened web search for 'uk housing'"
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        if name == "browser_scroll":
            return json.dumps({"ok": True, "at_bottom": False, "message": "scrolled"})
        if name == "extract_structured_data":
            extract_calls += 1
            if extract_calls == 1:
                return json.dumps(
                    {
                        "ok": False,
                        "message": "No structured items matched on the current page.",
                        "error_code": "empty_items",
                        "url": "https://www.google.com/search?q=uk+housing",
                        "title": "Google",
                    }
                )
            return json.dumps(
                {
                    "url": "https://www.google.com/search?q=uk+housing",
                    "title": "Google",
                    "item_type": "results",
                    "items": [{"label": "Housing overview", "href": "https://example.com/housing"}],
                    "item_count": 1,
                }
            )
        if name == "web_scrape":
            raise AssertionError("browser search should not fall back to background fetch after a weak extraction")
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _fake_route_decider
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(await get_web_information(query="uk housing", target_type="search_results"))
        assert payload["ok"] is True
        assert payload["route"] == "browser_aci"
        assert payload["search_strategy"] == "browser_live"
        assert payload["search_attempts"] == 2
        assert any(name == "browser_scroll" for name, _ in calls)
        assert extract_calls == 2
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        tool_registry.execute = original_execute


def test_get_web_information_reuses_selected_source_within_request():
    async def run():
        runtime_state_store.reset()
        runtime_state_store.start_request(query="apartments for rent in Egham UK")
        runtime_state_store.update_request_state(
            selected_source_url="https://www.zoopla.co.uk/to-rent/flats/egham/",
            selected_source_label="Flats and apartments to rent in Egham - Zoopla",
        )
        set_tool_gateway_context(active_app="Google Chrome", browser_url="https://www.google.com/search?q=egham+flats", background_mode=False)
        original_execute = tool_registry.execute
        original_route_decider = selector_module.decide_web_route_with_flash
        calls = []

        async def _fake_route_decider(**kwargs):
            return {
                "route": "browser_aci",
                "reason": "Live browser route for active search flow.",
                "confidence": 0.94,
                "_interpreter_model": "deterministic-web-policy",
            }

        async def _tracking_execute(name, args):
            calls.append((name, dict(args) if isinstance(args, dict) else args))
            if name == "open_url":
                return "opened"
            if name == "browser_refresh_refs":
                return json.dumps({"ok": True})
            if name == "extract_structured_data":
                return json.dumps(
                    {
                        "ok": True,
                        "url": "https://www.zoopla.co.uk/to-rent/flats/egham/",
                        "title": "Flats and apartments to rent in Egham - Zoopla",
                        "items": [
                            {"label": "£1,650 pcm Maxwell Mews, Egham", "href": "https://www.zoopla.co.uk/to-rent/details/70873203/"}
                        ],
                        "item_count": 1,
                        "extraction_strategy": "deterministic-property-cards",
                        "source_domain": "zoopla.co.uk",
                    }
                )
            if name == "web_search":
                raise AssertionError("Cached selected source should be reused before starting a new search")
            return await original_execute(name, args)

        selector_module.decide_web_route_with_flash = _fake_route_decider
        tool_registry.execute = _tracking_execute
        try:
            payload = json.loads(
                await get_web_information(
                    query="apartments for rent in Egham UK",
                    target_type="structured_data",
                    item_hint="apartment details including price, location, bedrooms, and features",
                    max_items=5,
                )
            )
            assert payload["ok"] is True
            assert payload["selected_source_url"] == "https://www.zoopla.co.uk/to-rent/flats/egham/"
            assert payload["search_follow_strategy"] == "request-state-reuse"
            assert any(name == "extract_structured_data" for name, _ in calls)
            assert all(name != "web_search" for name, _ in calls)
        finally:
            selector_module.decide_web_route_with_flash = original_route_decider
            tool_registry.execute = original_execute

    asyncio.run(run())


async def test_get_web_information_browser_search_flash_timeout_fails_fast():
    set_tool_gateway_context(active_app="Google Chrome", browser_url="https://www.google.com", background_mode=False)
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash

    async def _fake_route_decider(**kwargs):
        return {
            "route": "browser_aci",
            "reason": "Use live browser search flow.",
            "confidence": 0.94,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    calls = []

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "web_search":
            return "Opened web search for 'best comedy videos'"
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        if name == "extract_structured_data":
            return json.dumps(
                {
                    "ok": False,
                    "message": "Flash browser interpreter timed out after 10.0s.",
                    "error_code": "flash_timeout",
                    "url": "https://www.google.com/search?q=best+comedy+videos",
                    "title": "Google",
                }
            )
        if name == "browser_scroll":
            raise AssertionError("Flash timeout should fail fast instead of scrolling and retrying.")
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _fake_route_decider
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(await get_web_information(query="best comedy videos", target_type="search_results"))
        assert payload["ok"] is False
        assert payload["error_code"] == "flash_timeout"
        assert payload["search_attempts"] == 1
        assert not any(name == "browser_scroll" for name, _ in calls)
        assert len([name for name, _ in calls if name == "extract_structured_data"]) == 1
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        tool_registry.execute = original_execute


async def test_get_web_information_flash_background_route_for_explicit_url():
    set_tool_gateway_context(
        active_app="Google Chrome",
        browser_url="https://www.example.com/",
        background_mode=False,
    )
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash

    async def _fake_route_decider(**kwargs):
        return {
            "route": "background_fetch",
            "reason": "Direct source page read is more reliable via background fetch than the active browser tab.",
            "confidence": 0.95,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    calls = []

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "web_scrape":
            return json.dumps(
                {
                    "ok": True,
                    "url": "https://commonslibrary.parliament.uk/research-briefings/cbp-10567/",
                    "title": "Home ownership and renting",
                    "content": "This briefing covers social housing, private renting, and homeownership trends.",
                    "content_length": 83,
                    "links": [],
                    "link_count": 0,
                }
            )
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _fake_route_decider
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(
            await get_web_information(
                url="https://commonslibrary.parliament.uk/research-briefings/cbp-10567/",
                target_type="page_content",
            )
        )
        assert payload["ok"] is True
        assert payload["route"] == "background_fetch"
        assert payload["route_decision_model"] == "gemini-3-flash-preview"
        assert payload["content"].startswith("This briefing covers")
        assert all(name != "read_page_content" for name, _ in calls)
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        tool_registry.execute = original_execute


async def test_get_web_information_clicks_visible_search_result_before_reading():
    set_tool_gateway_context(
        active_app="Google Chrome",
        browser_url="https://www.google.com/search?q=uk+housing+review",
        background_mode=False,
    )
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash

    async def _route_decider_should_not_matter(**kwargs):
        return {
            "route": "background_fetch",
            "reason": "Direct page read",
            "confidence": 0.2,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    calls = []

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "extract_structured_data":
            return json.dumps(
                {
                    "ok": True,
                    "url": "https://www.google.com/search?q=uk+housing+review",
                    "title": "Google",
                    "items": [
                        {
                            "ref_id": "mw_result_1",
                            "label": "UK Housing Review - CIH",
                            "href": "https://www.cih.org/knowledge-hub/uk-housing-review/",
                            "context": "Annual analysis of the UK housing sector.",
                            "rank": 1,
                        }
                    ],
                    "item_count": 1,
                }
            )
        if name == "browser_click_ref":
            assert args == {"ref_id": "mw_result_1"}
            return json.dumps({"ok": True, "message": "clicked", "ref_id": "mw_result_1"})
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        if name == "get_page_summary":
            return json.dumps(
                {
                    "ok": True,
                    "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
                    "title": "UK Housing Review - the housing sector's annual analysis",
                    "summary": "The page provides annual analysis of the UK housing sector across supply, affordability, and tenure.",
                    "headings": [{"text": "Annual housing analysis", "tag": "h2", "ref_id": ""}],
                    "page_type": "report",
                    "total_elements": 6,
                    "item_count": 1,
                }
            )
        if name == "open_url":
            raise AssertionError("Should click the visible search result instead of opening the URL directly")
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _route_decider_should_not_matter
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(
            await get_web_information(
                url="https://www.cih.org/knowledge-hub/uk-housing-review/",
                target_type="page_summary",
            )
        )
        assert payload["ok"] is True
        assert payload["route"] == "browser_aci"
        assert payload["route_decision_model"] == "search-follow-policy"
        assert payload["summary"].startswith("The page provides annual analysis")
        assert any(name == "browser_click_ref" for name, _ in calls)
        assert any(name == "get_page_summary" for name, _ in calls)
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        tool_registry.execute = original_execute


async def test_get_web_information_background_page_summary_uses_flash_summary():
    set_tool_gateway_context(
        active_app="Google Chrome",
        browser_url="https://www.example.com/",
        background_mode=False,
    )
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash
    original_summary = selector_module.summarize_scraped_page_with_flash

    async def _fake_route_decider(**kwargs):
        return {
            "route": "background_fetch",
            "reason": "Direct source page read is more reliable via background fetch than the active browser tab.",
            "confidence": 0.95,
            "_interpreter_model": "gemini-3-flash-preview",
        }

    async def _fake_summary(**kwargs):
        return {
            "page_type": "policy briefing",
            "summary": "The briefing explains UK tenure patterns across owner occupation, private renting, and social housing.",
            "headings": [{"ref_id": "", "text": "Key tenure categories", "tag": "h2"}],
            "key_targets": [],
            "confidence": 0.92,
        }

    async def _tracking_execute(name, args):
        if name == "web_scrape":
            return json.dumps(
                {
                    "ok": True,
                    "url": "https://commonslibrary.parliament.uk/research-briefings/cbp-10567/",
                    "title": "Home ownership and renting",
                    "content": "Cookies on example. Navigation. The real article content starts here with tenure data and trends.",
                    "content_length": 94,
                    "links": [{"label": "Data table", "url": "https://example.com/table"}],
                    "link_count": 1,
                }
            )
        return await original_execute(name, args)

    selector_module.decide_web_route_with_flash = _fake_route_decider
    selector_module.summarize_scraped_page_with_flash = _fake_summary
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(
            await get_web_information(
                url="https://commonslibrary.parliament.uk/research-briefings/cbp-10567/",
                target_type="page_summary",
            )
        )
        assert payload["ok"] is True
        assert payload["route"] == "background_fetch"
        assert payload["summary"].startswith("The briefing explains UK tenure patterns")
        assert payload["page_type"] == "policy briefing"
        assert payload["content"].startswith("The briefing explains UK tenure patterns")
        assert payload["item_count"] == 1
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        selector_module.summarize_scraped_page_with_flash = original_summary
        tool_registry.execute = original_execute


async def test_get_web_information_degraded_route_uses_background_for_explicit_url():
    set_tool_gateway_context(
        active_app="Google Chrome",
        browser_url="https://www.example.com/",
        background_mode=False,
    )
    original_execute = tool_registry.execute
    original_route_decider = selector_module.decide_web_route_with_flash
    original_summary = selector_module.summarize_scraped_page_with_flash

    async def _failing_route_decider(**kwargs):
        raise selector_module.BrowserInterpretationError(
            "Flash browser interpreter timed out after 10.0s.",
            error_code="flash_timeout",
        )

    async def _fake_summary(**kwargs):
        return {
            "page_type": "sector review",
            "summary": "The page provides annual analysis of the UK housing sector across supply, affordability, and tenure.",
            "headings": [{"ref_id": "", "text": "Annual analysis", "tag": "h2"}],
            "key_targets": [],
            "confidence": 0.88,
        }

    calls = []

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "web_scrape":
            return json.dumps(
                {
                    "ok": True,
                    "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
                    "title": "UK Housing Review",
                    "content": "The UK Housing Review provides annual analysis of supply, affordability, and tenure trends.",
                    "content_length": 92,
                    "links": [],
                    "link_count": 0,
                }
            )
        raise AssertionError(f"Unexpected browser-path tool call: {name}")

    selector_module.decide_web_route_with_flash = _failing_route_decider
    selector_module.summarize_scraped_page_with_flash = _fake_summary
    tool_registry.execute = _tracking_execute
    try:
        payload = json.loads(
            await get_web_information(
                url="https://www.cih.org/knowledge-hub/uk-housing-review/",
                target_type="page_summary",
            )
        )
        assert payload["ok"] is True
        assert payload["route"] == "background_fetch"
        assert payload["route_decision_degraded"] is True
        assert payload["route_decision_error_code"] == "flash_timeout"
        assert payload["summary"].startswith("The page provides annual analysis")
        assert calls == [
            (
                "web_scrape",
                {
                    "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
                    "max_chars": 5000,
                    "include_links": True,
                },
            )
        ]
    finally:
        selector_module.decide_web_route_with_flash = original_route_decider
        selector_module.summarize_scraped_page_with_flash = original_summary
        tool_registry.execute = original_execute


async def test_execute_step_defers_research_commit_until_verification_success():
    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    step = ExecutionStep(
        id=1,
        description="Summarize page",
        tool="get_web_information",
        args={
            "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
            "target_type": "page_summary",
        },
    )

    original_execute = tool_registry.execute
    original_verify = agent.verifier.verify
    payload = json.dumps(
        {
            "ok": True,
            "target_type": "page_summary",
            "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
            "title": "UK Housing Review",
            "summary": "The review provides annual analysis of supply, affordability, tenure, regional trends, and delivery pressures across UK housing markets and policy.",
            "headings": [{"text": "Annual housing analysis", "tag": "h2", "ref_id": ""}],
            "page_type": "report",
            "content": "The review provides annual analysis of supply, affordability, tenure, regional trends, and delivery pressures across UK housing markets and policy.",
            "content_length": 148,
            "route": "background_fetch",
            "route_decision_degraded": True,
            "route_decision_reason": "Flash browser interpreter timed out after 10.0s.",
            "route_decision_error_code": "flash_timeout",
        }
    )

    async def _tracking_execute(name, args):
        if name == "get_web_information":
            return payload
        return await original_execute(name, args)

    async def _verify_fail(**kwargs):
        return VerificationResult(success=False, confidence=0.9, message="forced failure", should_retry=False)

    async def _verify_success(**kwargs):
        return VerificationResult(success=True, confidence=0.9, message="verified")

    tool_registry.execute = _tracking_execute
    try:
        agent.verifier.verify = _verify_fail
        ok = await agent._execute_step(step, WorldState(active_app="Google Chrome"), ws_callback=None)
        assert ok is False
        assert len(agent.working_memory.get_research_snippets()) == 0
        actions = agent.working_memory.get_recent_actions()
        assert len(actions) == 1
        assert actions[-1].success is False

        step = ExecutionStep(
            id=1,
            description="Summarize page",
            tool="get_web_information",
            args={
                "url": "https://www.cih.org/knowledge-hub/uk-housing-review/",
                "target_type": "page_summary",
            },
        )
        agent.verifier.verify = _verify_success
        ok = await agent._execute_step(step, WorldState(active_app="Google Chrome"), ws_callback=None)
        assert ok is True
        assert len(agent.working_memory.get_research_snippets()) == 1
        actions = agent.working_memory.get_recent_actions()
        assert len(actions) == 2
        assert actions[-1].success is True
    finally:
        tool_registry.execute = original_execute
        agent.verifier.verify = original_verify


def test_execute_step_uses_conservative_vision_recovery_for_ui_failure():
    async def run():
        agent = MoonwalkAgentV2(use_planning=False, persist=False)
        step = ExecutionStep(
            id=1,
            description="Click compose",
            tool="browser_click_match",
            args={"query": "Compose"},
        )

        original_execute = tool_registry.execute
        original_verify_with_visual = agent.verifier.verify_with_visual
        calls = []

        async def _tracking_execute(name, args):
            calls.append((name, dict(args) if isinstance(args, dict) else args))
            if name == "browser_click_match":
                return json.dumps({"ok": False, "message": "Could not find element", "error_code": "element_not_found"})
            if name == "read_screen":
                return "The Compose button is visible around coordinates (420, 180)."
            if name == "click_element":
                return "Clicked at (420, 180)"
            return await original_execute(name, args)

        async def _verify_with_visual(**kwargs):
            if kwargs["tool_name"] == "browser_click_match":
                return VerificationResult(
                    success=False,
                    confidence=0.95,
                    message="browser_click_match failed: Could not find element",
                    should_retry=True,
                )
            if kwargs["tool_name"] == "click_element":
                return VerificationResult(success=True, confidence=0.9, message="vision recovery click verified")
            return VerificationResult(success=True, confidence=0.9, message="ok")

        tool_registry.execute = _tracking_execute
        agent.verifier.verify_with_visual = _verify_with_visual
        try:
            ok = await agent._execute_step(step, WorldState(active_app="Google Chrome"), ws_callback=None)
            assert ok is True
            call_names = [name for name, _ in calls]
            assert "browser_click_match" in call_names
            assert "read_screen" in call_names
            assert "click_element" in call_names
            assert call_names.index("read_screen") > call_names.index("browser_click_match")
            assert call_names.index("click_element") > call_names.index("read_screen")
        finally:
            tool_registry.execute = original_execute
            agent.verifier.verify_with_visual = original_verify_with_visual

    asyncio.run(run())


def test_working_memory_dedupes_research_snippets():
    memory = WorkingMemory()
    memory.log_research_snippet(
        source="https://example.com/housing",
        title="Housing Overview",
        content="Owner occupation, private renting, and social housing are the main tenure types in the UK." * 4,
        tool="get_web_information",
    )
    memory.log_research_snippet(
        source="https://example.com/housing",
        title="Housing Overview",
        content="Owner occupation, private renting, and social housing are the main tenure types in the UK." * 4,
        tool="get_web_information",
    )
    assert len(memory.get_research_snippets()) == 1


class _FakeProvider:
    @property
    def name(self):
        return "fake"

    @property
    def supports_vision(self):
        return False

    @property
    def supports_tools(self):
        return False

    async def generate(self, messages, system_prompt, tools, image_data=None, temperature=0.7):
        return LLMResponse(
            text=(
                "## Housing Systems in the UK\n"
                "### Overview\n"
                "The UK housing landscape includes owner-occupation, private renting, and social housing.\n"
                "### Key Systems\n"
                "- Owner-occupied housing\n"
                "- Private rented sector\n"
                "- Social housing (council and housing associations)\n"
            )
        )

    async def is_available(self):
        return True


def test_non_research_document_body_synthesis_from_title():
    async def run():
        agent = MoonwalkAgentV2(use_planning=False, persist=False)
        synthesized = await agent._synthesize_document_body(
            provider=_FakeProvider(),
            title="Fascinating Facts About Octopuses",
            user_text="proceed",
            task_summary="Create a Google Document about a random topic",
        )
        assert synthesized
        assert "Housing Systems in the UK" in synthesized
        assert agent._looks_like_contentful_doc_task("proceed", "Create a Google Document about a random topic") is True

    asyncio.run(run())


async def test_template_pack_tool_filter_fallback():
    class _MilestoneProvider:
        async def generate(self, messages, system_prompt, tools, temperature=0.1):
            return LLMResponse(
                text=json.dumps(
                    {
                        "task_summary": "Research UK housing systems and write a Google document",
                        "needs_clarification": False,
                        "milestones": [
                            {
                                "id": 1,
                                "goal": "Research UK housing systems",
                                "success_signal": "Research notes captured",
                                "hint_tools": ["browser_read_page", "gdocs_create"],
                                "depends_on": [],
                                "deliverable_key": "research_notes",
                            },
                            {
                                "id": 2,
                                "goal": "Create the Google document",
                                "success_signal": "Google Doc URL is available",
                                "hint_tools": ["gdocs_create"],
                                "depends_on": [1],
                                "deliverable_key": "doc_url",
                            },
                        ],
                        "final_response": "Done!",
                    }
                )
            )

    planner = TaskPlanner(provider=_MilestoneProvider(), tool_registry=tool_registry)
    world_state = WorldState(active_app="Google Chrome", browser_url="https://www.google.com")
    plan = await planner.create_plan(
        user_request="research UK housing systems and write a Google document",
        world_state=world_state,
        available_tools=["open_url", "gdocs_create"],  # intentionally narrow (missing browser_read_page)
    )
    assert plan.source == "milestone_planner"
    assert plan.skill_context in ("", "(none)") or "research_to_document_skill" in plan.skill_context
    hinted_tools = {
        tool
        for milestone in plan.milestones
        for tool in milestone.hint_tools
    }
    assert "gdocs_create" in hinted_tools


def test_plan_gate_policy():
    agent = MoonwalkAgentV2(use_planning=False, persist=False)

    high_risk = _milestone_plan("Run command", ["run_shell"])
    assert agent._should_gate_plan(high_risk) is True

    read_only = _milestone_plan("Read only", ["read_file"])
    assert agent._should_gate_plan(read_only) is False


def test_pending_plan_staleness_rules():
    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    plan = _milestone_plan("Draft plan", ["open_url"])

    old_pending = PendingPlanState(
        plan_id="old123",
        plan=plan,
        created_at=time.time() - 700,
        context_fingerprint={"active_app": "google chrome", "browser_domain": "example.com"},
        provider=None,
        original_user_request="open example",
    )
    stale, reason = agent._is_pending_plan_stale(
        old_pending,
        ContextSnapshot(active_app="Google Chrome", browser_url="https://example.com", window_title="Example"),
    )
    assert stale is True
    assert "older" in reason

    app_drift_pending = PendingPlanState(
        plan_id="app123",
        plan=plan,
        created_at=time.time(),
        context_fingerprint={"active_app": "google chrome", "browser_domain": "example.com"},
        provider=None,
        original_user_request="open example",
    )
    stale, reason = agent._is_pending_plan_stale(
        app_drift_pending,
        ContextSnapshot(active_app="Terminal", browser_url=None, window_title="zsh"),
    )
    assert stale is True
    assert "active app changed" in reason

    web_plan = _milestone_plan(
        "Research and write",
        ["open_url"],
        ["browser_read_page"],
        ["gdocs_create"],
    )
    web_pending = PendingPlanState(
        plan_id="web123",
        plan=web_plan,
        created_at=time.time(),
        context_fingerprint={"active_app": "google chrome", "browser_domain": "google.com"},
        provider=None,
        original_user_request="research uk housing and write a document",
    )
    stale, reason = agent._is_pending_plan_stale(
        web_pending,
        ContextSnapshot(active_app="Electron", browser_url=None, window_title="Moonwalk"),
    )
    assert stale is False
    assert reason == ""


async def test_execute_step_handles_await_reply_payload():
    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    step = ExecutionStep(
        id=1,
        description="Ask for confirmation",
        tool="await_reply",
        args={"message": "Proceed?"},
    )

    ok = await agent._execute_step(
        step=step,
        world_state=WorldState(active_app="Terminal"),
        ws_callback=None,
        user_text="pause",
        task_summary="Pause for approval",
    )
    assert ok is True
    assert step.status.name == "COMPLETED"
    assert step.result.startswith("Proceed")


async def test_execute_milestone_plan_resumes_after_await_reply():
    class _ScriptedProvider:
        def __init__(self, responses):
            self._responses = list(responses)
            self.name = "scripted"

        async def generate(self, messages, system_prompt="", tools=None, temperature=0.0):
            return LLMResponse(text=self._responses.pop(0))

    provider = _ScriptedProvider([
        json.dumps({
            "done": False,
            "tool": "await_reply",
            "args": {"message": "Please paste the Luma link"},
            "reasoning": "Need the source before I can summarize the tasks",
            "description": "Ask for the source link",
        }),
        json.dumps({
            "done": False,
            "tool": "get_web_information",
            "args": {"target_type": "page_summary", "url": "https://lu.ma/encode-demo"},
            "reasoning": "Use the user-provided link to read the demo details",
            "description": "Read the source page",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "The demo tasks were extracted from the source page",
            "deliverable": "Introduce the demo, walk through the encode flow, and handle Q&A.",
        }),
    ])
    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    plan = MilestonePlan(
        task_summary="Find the encode demo tasks and message Kris",
        milestones=[
            Milestone(
                id=1,
                goal="Collect the encode demo tasks",
                success_signal="The demo tasks are extracted from the source",
                hint_tools=["await_reply", "get_web_information"],
                deliverable_key="demo_tasks",
            )
        ],
        final_response="Done!",
    )
    context = ContextSnapshot(active_app="WhatsApp", window_title="WhatsApp")
    llm_tool_declarations = [
        {
            "name": "await_reply",
            "description": "Ask the user a blocking question",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
        {
            "name": "get_web_information",
            "description": "Read a source page or search for information",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_type": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["target_type"],
            },
        },
        {
            "name": "send_response",
            "description": "Send the final response",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    ]

    async def _fake_perceive_environment(followup_inputs=None):
        env = {"active_app": "WhatsApp", "window_title": "WhatsApp"}
        if followup_inputs:
            env["user_followups"] = " || ".join(followup_inputs)
        return env

    async def _fake_execute_step(step, world_state, ws_callback, user_text="", task_summary=""):
        if step.tool == "await_reply":
            step.status = StepStatus.COMPLETED
            step.result = "Please paste the Luma link"
            step.modal_data = {"modal": "text", "message": "Please paste the Luma link"}
            return True
        if step.tool == "get_web_information":
            step.status = StepStatus.COMPLETED
            step.result = json.dumps({
                "target_type": "page_summary",
                "url": "https://lu.ma/encode-demo",
                "summary": "Introduce the demo, walk through the encode flow, and handle Q&A.",
                "content_length": 128,
            })
            return True
        raise AssertionError(f"Unexpected tool: {step.tool}")

    agent._perceive_environment = _fake_perceive_environment
    agent._execute_step = _fake_execute_step

    first_response, awaiting = await agent._execute_milestone_plan(
        plan=plan,
        world_state=WorldState(active_app="WhatsApp"),
        provider=provider,
        context=context,
        user_text="message kris with an overview of what i need to do during the encode demo",
        llm_tool_declarations=llm_tool_declarations,
        ws_callback=None,
    )
    assert awaiting is True
    assert "Luma link" in first_response
    pending_execution = agent._pending_execution
    assert pending_execution is not None
    assert pending_execution.suspended_milestone_id == 1

    agent._pending_execution = None
    final_response, awaiting = await agent._execute_milestone_plan(
        plan=plan,
        world_state=WorldState(active_app="WhatsApp"),
        provider=provider,
        context=context,
        user_text=(
            "message kris with an overview of what i need to do during the encode demo\n\n"
            "User follow-up:\nhttps://lu.ma/encode-demo"
        ),
        llm_tool_declarations=llm_tool_declarations,
        ws_callback=None,
        pending_execution=pending_execution,
        followup_inputs=["https://lu.ma/encode-demo"],
    )
    assert awaiting is False
    assert final_response == "Done!"
    assert plan.milestones[0].status.name == "COMPLETED"


async def test_await_reply_blocks_execution():
    await test_execute_milestone_plan_resumes_after_await_reply()


async def test_browser_read_page_context_recovery():
    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    agent._last_opened_url = "https://www.google.com/search?q=uk+housing"
    step = ExecutionStep(
        id=1,
        description="Read page",
        tool="browser_read_page",
        args={"query": "uk housing"},
    )

    calls = []
    original_execute = tool_registry.execute

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "browser_read_page" and isinstance(args, dict) and args.get("refresh"):
            return json.dumps({"url": "https://www.google.com/search?q=uk+housing", "title": "Google", "content": "results"})
        if name == "browser_read_page":
            return json.dumps({"url": "https://www.youtube.com/shorts/abc", "title": "YouTube", "content": "video"})
        if name == "browser_switch_tab":
            return json.dumps({"ok": True, "message": "switched"})
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        return await original_execute(name, args)

    tool_registry.execute = _tracking_execute
    try:
        ok = await agent._execute_step(step, WorldState(active_app="Google Chrome"), ws_callback=None)
        assert ok is True
        payload = json.loads(step.result)
        assert "google.com/search" in payload.get("url", "")
        assert any(name == "browser_switch_tab" for name, _ in calls)
        assert any(name == "browser_read_page" and isinstance(args, dict) and args.get("refresh") is True for name, args in calls)
    finally:
        tool_registry.execute = original_execute


async def test_extract_structured_data_context_recovery():
    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    agent._last_opened_url = "https://www.google.com/search?q=uk+housing"
    step = ExecutionStep(
        id=1,
        description="Extract results",
        tool="extract_structured_data",
        args={"item_type": "results"},
    )

    calls = []
    original_execute = tool_registry.execute

    async def _tracking_execute(name, args):
        calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "extract_structured_data":
            if any(call_name == "browser_switch_tab" for call_name, _ in calls):
                return json.dumps({
                    "url": "https://www.google.com/search?q=uk+housing",
                    "title": "Google",
                    "item_type": "results",
                    "items": [{"label": "UK housing overview", "href": "https://example.com/housing"}],
                    "item_count": 1,
                })
            return json.dumps({
                "url": "https://www.youtube.com/watch?v=abc",
                "title": "YouTube",
                "item_type": "results",
                "items": [{"label": "Video", "href": "https://youtube.com/watch?v=abc"}],
                "item_count": 1,
            })
        if name == "browser_switch_tab":
            return json.dumps({"ok": True, "message": "switched"})
        if name == "browser_refresh_refs":
            return json.dumps({"ok": True, "message": "refreshed"})
        return await original_execute(name, args)

    tool_registry.execute = _tracking_execute
    try:
        ok = await agent._execute_step(step, WorldState(active_app="Google Chrome"), ws_callback=None)
        assert ok is True
        payload = json.loads(step.result)
        assert "google.com/search" in payload.get("url", "")
        assert any(name == "browser_switch_tab" for name, _ in calls)
        assert sum(1 for name, _ in calls if name == "extract_structured_data") >= 2
    finally:
        tool_registry.execute = original_execute


async def test_high_risk_plan_gates_before_execution():
    class _PlanProvider:
        @property
        def name(self):
            return "fake-plan"

        async def generate(self, messages, system_prompt, tools, image_data=None, temperature=0.7):
            return LLMResponse(
                text=json.dumps(
                    {
                        "task_summary": "Replace foo with bar in app.py",
                        "needs_clarification": False,
                        "milestones": [
                            {
                                "id": 1,
                                "goal": "Update app.py to replace foo with bar",
                                "success_signal": "Replacement is applied to app.py",
                                "hint_tools": ["read_file", "replace_in_file"],
                                "depends_on": [],
                                "deliverable_key": "patched_file",
                            }
                        ],
                        "final_response": "Done!",
                    }
                )
            )

    agent = MoonwalkAgentV2(use_planning=False, persist=False)
    agent._pending_reply_provider = _PlanProvider()
    context = ContextSnapshot(active_app="Terminal", window_title="Terminal")

    calls = []
    original_execute = tool_registry.execute

    async def _tracking_execute(name, args):
        calls.append(name)
        if name == "await_reply":
            return "AWAIT:" + json.dumps({
                "modal": "plan",
                "message": "Review this plan",
                "steps": [{"label": "Example step"}],
                "plan_id": "test-plan-id",
            })
        return await original_execute(name, args)

    tool_registry.execute = _tracking_execute
    try:
        _, awaiting = await agent.run("replace foo with bar in app.py", context, ws_callback=None)
        assert awaiting is True
        assert calls == ["await_reply"]
        assert agent._pending_plan is not None
    finally:
        tool_registry.execute = original_execute


def test_template_registry_packs():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)
    assert planner.template_registry.size >= 4

    world_state = WorldState(active_app="Terminal")
    request = "replace foo with bar in app.py"
    intent = planner.intent_parser.parse(request, world_state)

    candidates = planner.template_registry.get_skill_candidates(
        user_request=request,
        intent=intent,
        world_state=world_state,
        available_tools=["read_file", "replace_in_file"],
    )
    assert candidates
    assert candidates[0].pack.name == "file_patch"
    context = planner.template_registry.format_skill_context(candidates)
    assert "file_patch" in context
    assert "replace_in_file" in context


def test_template_registry_context_scoring():
    with tempfile.TemporaryDirectory() as tmp_dir:
        generic_pack = {
            "name": "generic_doc",
            "priority": 100,
            "match": {
                "intent_actions": ["create", "search", "unknown"],
                "keywords_any": ["report", "document"],
            },
            "plan": {
                "task_summary": "Generic document workflow",
                "steps": [
                    {
                        "description": "Open search page",
                        "tool": "open_url",
                        "args": {"url": "https://www.google.com/search?q={query_urlencoded}"},
                    }
                ],
            },
            "constraints": {"required_tools": ["open_url"]},
            "final_response": "generic",
        }
        browser_specific_pack = {
            "name": "browser_specific_doc",
            "priority": 100,
            "match": {
                "intent_actions": ["create", "search", "unknown"],
                "keywords_any": ["report", "document"],
                "require_browser": True,
                "active_apps_any": ["google chrome"],
            },
            "plan": {
                "task_summary": "Browser-specific document workflow",
                "steps": [
                    {
                        "description": "Read browser page",
                        "tool": "browser_read_page",
                        "args": {"query": "{query}"},
                    }
                ],
            },
            "constraints": {"required_tools": ["browser_read_page"]},
            "final_response": "browser",
        }

        with open(os.path.join(tmp_dir, "a_generic.json"), "w", encoding="utf-8") as f:
            json.dump(generic_pack, f)
        with open(os.path.join(tmp_dir, "b_browser.json"), "w", encoding="utf-8") as f:
            json.dump(browser_specific_pack, f)

        registry = TemplateRegistry(packs_dir=tmp_dir)
        world_state = WorldState(
            active_app="Google Chrome",
            browser_url="https://example.com/page",
        )
        intent = TaskPlanner(provider=None, tool_registry=tool_registry).intent_parser.parse(
            "create a report document about uk housing",
            world_state,
        )
        candidates = registry.get_skill_candidates(
            user_request="create a report document about uk housing",
            intent=intent,
            world_state=world_state,
            available_tools=["open_url", "browser_read_page"],
        )
        assert candidates
        assert candidates[0].pack.name == "browser_specific_doc"
        context = registry.format_skill_context(candidates)
        assert "browser_specific_doc" in context
        assert "browser_read_page" in context


def test_template_registry_skill_semantic_routing():
    with tempfile.TemporaryDirectory() as tmp_dir:
        generic_pack = {
            "name": "generic_research",
            "priority": 110,
            "match": {
                "intent_actions": ["query", "search", "unknown"],
                "keywords_any": ["research"],
            },
            "plan": {
                "task_summary": "Generic research",
                "steps": [
                    {
                        "description": "Open search",
                        "tool": "open_url",
                        "args": {"url": "https://www.google.com/search?q={query_urlencoded}"},
                    }
                ],
            },
            "constraints": {"required_tools": ["open_url"]},
            "final_response": "generic",
        }
        skill_pack = {
            "name": "research_skill_pack",
            "priority": 110,
            "skill": {
                "description": "Research UK housing and local flats from web sources.",
                "examples": [
                    "can you research uk flats in egham",
                    "research student flats near egham",
                ],
                "capabilities_all": ["research", "browser_read"],
                "min_semantic_score": 0.15,
                "semantic_weight": 10.0,
            },
            "match": {
                "intent_actions": ["query", "search", "unknown"],
                "keywords_any": ["research"],
            },
            "plan": {
                "task_summary": "Skill research",
                "steps": [
                    {
                        "description": "Open search",
                        "tool": "open_url",
                        "args": {"url": "https://www.google.com/search?q={query_urlencoded}"},
                    },
                    {
                        "description": "Read page",
                        "tool": "browser_read_page",
                        "args": {"query": "{query}"},
                    }
                ],
            },
            "constraints": {"required_tools": ["open_url", "browser_read_page"]},
            "final_response": "skill",
        }

        with open(os.path.join(tmp_dir, "a_generic.json"), "w", encoding="utf-8") as f:
            json.dump(generic_pack, f)
        with open(os.path.join(tmp_dir, "b_skill.json"), "w", encoding="utf-8") as f:
            json.dump(skill_pack, f)

        registry = TemplateRegistry(packs_dir=tmp_dir)
        world_state = WorldState(active_app="Google Chrome", browser_url="https://www.google.com")
        intent = TaskPlanner(provider=None, tool_registry=tool_registry).intent_parser.parse(
            "can you research uk flats in egham",
            world_state,
        )
        candidates = registry.get_skill_candidates(
            user_request="can you research uk flats in egham",
            intent=intent,
            world_state=world_state,
            available_tools=["open_url", "browser_read_page"],
        )
        assert candidates
        assert candidates[0].pack.name == "research_skill_pack"
        context = registry.format_skill_context(candidates)
        assert "browser_read_page" in context


def test_template_registry_skill_capability_guard():
    with tempfile.TemporaryDirectory() as tmp_dir:
        doc_only_pack = {
            "name": "doc_only_pack",
            "priority": 120,
            "skill": {
                "description": "Research then create document output",
                "examples": ["research and create a report"],
                "capabilities_all": ["research", "document_output"],
                "min_semantic_score": 0.1,
            },
            "match": {
                "intent_actions": ["query", "search", "create", "unknown"],
                "keywords_any": ["research"],
            },
            "plan": {
                "task_summary": "Doc output flow",
                "steps": [
                    {
                        "description": "Open search",
                        "tool": "open_url",
                        "args": {"url": "https://www.google.com/search?q={query_urlencoded}"},
                    }
                ],
            },
            "constraints": {"required_tools": ["open_url"]},
            "final_response": "doc",
        }
        with open(os.path.join(tmp_dir, "doc_only.json"), "w", encoding="utf-8") as f:
            json.dump(doc_only_pack, f)

        registry = TemplateRegistry(packs_dir=tmp_dir)
        world_state = WorldState(active_app="Google Chrome", browser_url="https://www.google.com")
        intent = TaskPlanner(provider=None, tool_registry=tool_registry).intent_parser.parse(
            "can you research uk flats in egham",
            world_state,
        )
        candidates = registry.get_skill_candidates(
            user_request="can you research uk flats in egham",
            intent=intent,
            world_state=world_state,
            available_tools=["open_url"],
        )
        assert candidates == []


def test_research_brief_pack_match():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)
    world_state = WorldState(active_app="Google Chrome", browser_url="https://www.google.com")
    request = "can you research uk flats in egham"
    intent = planner.intent_parser.parse(request, world_state)
    candidates = planner.template_registry.get_skill_candidates(
        user_request=request,
        intent=intent,
        world_state=world_state,
        available_tools=["open_url", "web_search", "browser_read_page", "browser_click_match", "browser_scroll"],
    )
    assert candidates
    assert candidates[0].pack.name == "research_brief"
    context = planner.template_registry.format_skill_context(candidates)
    assert any(tool in context for tool in ("open_url", "web_search"))
    assert "browser_read_page" in context


async def main():
    await test_response_contract_aliases()
    print("✓ response tool aliases")
    await test_response_contract_cards_payloads()
    print("✓ response cards payloads")
    await test_file_tools_reliability()
    print("✓ file tool reliability")
    test_selector_coverage()
    print("✓ selector coverage")
    test_selector_uses_clipboard_source_for_overview_request()
    print("✓ selector clipboard context")
    test_selector_media_fast_path_ignores_research_context()
    print("✓ selector media fast path")
    await _planner_media_shortcut_for_direct_open_requests_async()
    print("✓ planner media shortcut")
    test_plan_tool_contract_requires_supported_hints()
    print("✓ plan tool contract validation")
    test_planner_preflight_contracts()
    print("✓ planner preflight")
    test_research_document_template_bypass()
    print("✓ research-document template bypass")
    await test_research_document_fallback_writing()
    print("✓ research document writing fallback")
    await test_template_pack_tool_filter_fallback()
    print("✓ template pack fallback on narrow tool filter")
    test_plan_gate_policy()
    print("✓ plan gate policy")
    test_pending_plan_staleness_rules()
    print("✓ pending plan staleness")
    await test_await_reply_blocks_execution()
    print("✓ await_reply blocking")
    await test_execute_milestone_plan_resumes_after_await_reply()
    print("✓ milestone await_reply resume")
    await test_browser_read_page_context_recovery()
    print("✓ browser read_page context recovery")
    await test_extract_structured_data_context_recovery()
    print("✓ extract_structured_data context recovery")
    await test_high_risk_plan_gates_before_execution()
    print("✓ high-risk plan gating")
    test_template_registry_packs()
    print("✓ template registry packs")
    test_template_registry_context_scoring()
    print("✓ template registry scoring")
    test_template_registry_skill_semantic_routing()
    print("✓ template registry skill semantic routing")
    test_template_registry_skill_capability_guard()
    print("✓ template registry skill capability guard")
    test_research_brief_pack_match()
    print("✓ research brief pack match")
    await test_browser_error_payloads()
    print("✓ browser error payloads")
    await test_get_web_information_background_search_route()
    print("✓ get_web_information background route")
    await test_get_web_information_flash_browser_route_for_search()
    print("✓ get_web_information flash browser route")
    await test_get_web_information_live_bridge_failure_stays_on_browser_route()
    print("✓ get_web_information live bridge stays browser route")
    await test_get_web_information_query_page_summary_runs_search_follow_read_loop()
    print("✓ get_web_information query page_summary search-follow-read")
    await test_get_web_information_browser_search_scrolls_before_fallback()
    print("✓ get_web_information browser search retry")
    await test_get_web_information_browser_search_flash_timeout_fails_fast()
    print("✓ get_web_information browser search flash-timeout fast-fail")
    await test_get_web_information_flash_background_route_for_explicit_url()
    print("✓ get_web_information flash background route")
    await test_get_web_information_clicks_visible_search_result_before_reading()
    print("✓ get_web_information clicks visible search result")
    await test_get_web_information_background_page_summary_uses_flash_summary()
    print("✓ get_web_information background page summary")
    await test_get_web_information_degraded_route_uses_background_for_explicit_url()
    print("✓ get_web_information degraded explicit-url fallback")
    await test_browser_refresh_refs_bootstraps_initial_snapshot()
    print("✓ browser_refresh_refs bootstrap")
    await test_execute_step_defers_research_commit_until_verification_success()
    print("✓ execute_step defers research commit until verification")
    test_working_memory_dedupes_research_snippets()
    print("✓ working memory research dedupe")
    print("All reliability recovery checks passed")


if __name__ == "__main__":
    asyncio.run(main())
