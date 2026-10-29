"""
OpenClaw-aligned planner regression tests.
"""

import asyncio
import json
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from agent.core_v2 import MoonwalkAgentV2
from agent.planner import Milestone, MilestonePlan
from agent.task_planner import TaskPlanner
from agent.verifier import ToolVerifier
from agent.world_state import IntentParser, WorldState
from providers.router import ModelRouter
from tools import registry as tool_registry
from tools.mac_tools import _candidate_app_names, _match_installed_app_name
from tools.selector import ToolSelector


COMPOUND_MEDIA_REQUEST = (
    "can you help me edit my capcut video, "
    "i want to edit the latest video in my downloads folder"
)


def test_task_graph_extracts_compound_local_media_entities():
    parser = IntentParser()

    graph = parser.extract_task_graph(COMPOUND_MEDIA_REQUEST)

    assert graph.primary_action == "modify"
    assert {"app", "folder", "content"}.issubset(graph.entity_types())
    assert any(entity.type == "app" and entity.value == "CapCut" for entity in graph.entities)
    assert any(entity.type == "folder" and entity.value == "Downloads" for entity in graph.entities)
    assert any(entity.type == "content" and entity.value == "video" for entity in graph.entities)
    assert "latest" in graph.selectors
    assert "specific_edit_instructions" in graph.unresolved_slots
    assert graph.complexity_score >= 3.0


def test_compound_task_bypasses_template_shortcuts():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)
    graph = planner.intent_parser.extract_task_graph(COMPOUND_MEDIA_REQUEST)

    assert planner._should_bypass_template_shortcuts(COMPOUND_MEDIA_REQUEST, graph) is True


def test_should_use_milestones_for_compound_app_file_task():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)
    graph = planner.intent_parser.extract_task_graph(COMPOUND_MEDIA_REQUEST)

    assert planner.should_use_milestones(COMPOUND_MEDIA_REQUEST, task_graph=graph) is True


def test_tool_selector_retains_file_system_for_capcut_downloads_request():
    selector = ToolSelector()

    tools = selector.select(
        COMPOUND_MEDIA_REQUEST,
        context_app="Google Chrome",
        context_url="",
    )

    assert "open_app" in tools
    assert "list_directory" in tools
    assert "run_shell" in tools


def test_router_fast_path_detects_trivial_request():
    router = ModelRouter()

    assert router._looks_trivial_fast_request("open spotify") is True
    assert router._looks_trivial_fast_request(COMPOUND_MEDIA_REQUEST) is False
    assert router._looks_trivial_fast_request("proceed") is False


def test_simple_open_app_plan_is_not_gated():
    agent = MoonwalkAgentV2(use_planning=True, persist=False)
    simple_plan = MilestonePlan(
        task_summary="Open Spotify",
        milestones=[
            Milestone(
                id=1,
                goal="Open Spotify",
                success_signal="Spotify is open",
                hint_tools=["open_app"],
            )
        ],
    )

    assert agent._should_gate_plan(simple_plan) is False


def test_candidate_app_names_include_alias_resolution():
    candidates = _candidate_app_names("chrome")

    assert "Google Chrome" in candidates


def test_match_installed_app_name_uses_local_application_listing(monkeypatch):
    monkeypatch.setattr("tools.mac_tools.os.path.isdir", lambda path: path == "/Applications")
    monkeypatch.setattr(
        "tools.mac_tools.os.listdir",
        lambda path: ["Spotify.app", "Google Chrome.app"] if path == "/Applications" else [],
    )

    matched = asyncio.run(_match_installed_app_name("spotify"))

    assert matched == "Spotify"


def test_verify_open_app_accepts_launched_when_state_matches():
    verifier = ToolVerifier()

    async def get_state():
        return {"active_app": "CapCut"}

    result = asyncio.run(
        verifier._verify_open_app(
            {"app_name": "CapCut"},
            "Launched CapCut. It may already have been open — use Cmd+Tab if it's not visible.",
            "",
            get_state,
        )
    )

    assert result.success is True


def test_verify_open_app_rejects_missing_native_app():
    verifier = ToolVerifier()

    result = asyncio.run(
        verifier._verify_open_app(
            {"app_name": "Spotify"},
            "Couldn't find 'Spotify' as an installed app",
            "",
            None,
        )
    )

    assert result.success is False


def test_template_registry_surfaces_skill_context_for_research_request():
    planner = TaskPlanner(provider=None, tool_registry=tool_registry)

    intent = planner.intent_parser.parse(
        "research all UK housing and create a detailed google document about it"
    )
    candidates = planner.template_registry.get_skill_candidates(
        user_request="research all UK housing and create a detailed google document about it",
        intent=intent,
        world_state=WorldState(active_app="Google Chrome"),
        available_tools=None,
    )
    context = planner.template_registry.format_skill_context(candidates)

    assert candidates
    assert "research_to_document_skill" in context
    assert "Suggested tools" in context


def test_compound_request_uses_skill_overlay_not_direct_pack():
    class _FakeProvider:
        async def generate(self, messages, system_prompt, tools, temperature=0.1):
            return type(
                "Resp",
                (),
                {
                    "text": json.dumps(
                        {
                            "task_summary": "Research and document UK housing",
                            "needs_clarification": False,
                            "clarification_prompt": "",
                            "milestones": [
                                {
                                    "id": 1,
                                    "goal": "Gather reliable research on UK housing systems",
                                    "success_signal": "Relevant source pages and notes collected",
                                    "hint_tools": ["web_search", "browser_read_page"],
                                    "depends_on": [],
                                    "deliverable_key": "research_notes",
                                },
                                {
                                    "id": 2,
                                    "goal": "Create a Google document with the findings",
                                    "success_signal": "Document URL returned",
                                    "hint_tools": ["gdocs_create"],
                                    "depends_on": [1],
                                    "deliverable_key": "doc_url",
                                },
                            ],
                            "final_response": "Done",
                        }
                    )
                },
            )()

    planner = TaskPlanner(provider=_FakeProvider(), tool_registry=tool_registry)
    plan = asyncio.run(
        planner.create_plan(
            "research all UK housing and create a detailed google document about it",
            WorldState(active_app="Google Chrome"),
            available_tools=["web_search", "browser_read_page", "gdocs_create"],
        )
    )

    assert plan.source == "milestone_planner"
    assert len(plan.milestones) == 2
    assert "research_to_document_skill" in plan.skill_context
