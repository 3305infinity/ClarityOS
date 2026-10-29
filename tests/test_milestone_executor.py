"""
Test Stage 5: Milestone micro-loop executor.
"""
import sys
import os
import json
import asyncio

backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from agent.milestone_executor import (
    MilestoneExecutor,
    MilestoneAction,
    MILESTONE_EXECUTOR_SYSTEM,
    MILESTONE_EXECUTOR_PROMPT,
)
from agent.planner import Milestone, MilestonePlan, MilestoneStatus


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.tool_calls = []


class FakeLLM:
    """Fake LLM provider that returns scripted responses."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._call_count = 0
        self.name = "fake"

    async def generate(self, messages, system_prompt="", tools=None, temperature=0.0):
        if self._call_count < len(self._responses):
            text = self._responses[self._call_count]
            self._call_count += 1
            return FakeResponse(text)
        return FakeResponse('{"done": true, "tool": "", "args": {}, "reasoning": "fallback done"}')


def _make_plan(milestones: list[Milestone], task_summary: str = "Test task") -> MilestonePlan:
    return MilestonePlan(
        task_summary=task_summary,
        milestones=milestones,
        final_response="Task complete.",
    )


FAKE_TOOL_DECLS = [
    {
        "name": "get_web_information",
        "description": "Read or search web information",
        "parameters": {
            "type": "object",
            "properties": {
                "target_type": {"type": "string"},
                "query": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["target_type"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "browser_read_page",
        "description": "Read page content",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "gdocs_create",
        "description": "Create a Google Doc",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "open_url",
        "description": "Open a URL",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
]


# ═══════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════

def test_milestone_executor_init():
    """MilestoneExecutor initializes and builds tool list (hides raw browser tools)."""
    llm = FakeLLM([])
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    assert "web_search" in executor._tool_list_cache
    # browser_read_page is a raw browser tool — should be hidden from milestone LLM
    assert "browser_read_page" not in executor._tool_list_cache
    # reasoning param should be excluded from display
    assert "reasoning" not in executor._tool_list_cache


def test_tool_list_cache_shows_enum_values():
    llm = FakeLLM([])
    executor = MilestoneExecutor(
        llm,
        FAKE_TOOL_DECLS + [
            {
                "name": "extract_structured_data",
                "description": "Extract structured page items",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_type": {
                            "type": "string",
                            "enum": ["links", "results", "products"],
                        }
                    },
                    "required": ["item_type"],
                },
            }
        ],
    )

    assert "item_type*:{links|results|products}" in executor._tool_list_cache


def test_parse_decision_valid_json():
    """Parser handles clean JSON."""
    executor = MilestoneExecutor(FakeLLM([]), FAKE_TOOL_DECLS)
    d = executor._parse_decision('{"done": false, "tool": "web_search", "args": {"query": "test"}, "reasoning": "need data"}')
    assert d["tool"] == "web_search"
    assert d["args"]["query"] == "test"
    assert d["done"] is False


def test_parse_decision_code_fenced():
    """Parser strips markdown code fences."""
    executor = MilestoneExecutor(FakeLLM([]), FAKE_TOOL_DECLS)
    raw = '```json\n{"done": true, "tool": "", "args": {}, "reasoning": "all done"}\n```'
    d = executor._parse_decision(raw)
    assert d["done"] is True


def test_parse_decision_embedded_json():
    """Parser extracts JSON from surrounding text."""
    executor = MilestoneExecutor(FakeLLM([]), FAKE_TOOL_DECLS)
    raw = 'Here is my decision: {"done": false, "tool": "web_search", "args": {"query": "x"}} end'
    d = executor._parse_decision(raw)
    assert d["tool"] == "web_search"


def test_parse_decision_garbage():
    """Parser returns safe fallback for unparseable text."""
    executor = MilestoneExecutor(FakeLLM([]), FAKE_TOOL_DECLS)
    d = executor._parse_decision("this is not json at all")
    assert d["tool"] == ""
    assert d["done"] is False


def test_execute_milestone_done_immediately():
    """LLM declares milestone done on first action → success."""
    llm = FakeLLM([json.dumps({
        "done": True,
        "tool": "",
        "args": {},
        "reasoning": "Already have the data",
        "deliverable": "some result",
    })])
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(id=1, goal="Get data", success_signal="data acquired")

    async def env_perceiver():
        return {"active_app": "Chrome", "browser_url": "https://example.com"}

    async def tool_executor(tool, args):
        return ("result", True)

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
        )
    )
    assert success is True
    assert "some result" in result


def test_execute_milestone_done_requires_evidence_for_research_goal():
    """Research-style milestones should reject 'done' until evidence is collected."""
    responses = [
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "The search page is open",
            "deliverable": "Research complete",
        }),
        json.dumps({
            "done": False,
            "tool": "web_search",
            "args": {"query": "uk housing systems"},
            "reasoning": "Need to gather evidence first",
            "description": "Search for sources",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "Evidence collected from sources",
            "deliverable": "Collected source notes with housing system details",
        }),
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(
        id=1,
        goal="Research UK housing systems and identify supporting sources",
        success_signal="Source evidence and extracted notes collected",
    )

    tools_called = []

    async def env_perceiver():
        return {"active_app": "Google Chrome"}

    async def tool_executor(tool, args):
        tools_called.append((tool, args))
        return ("Detailed extracted notes about council housing, private renting, and ownership models.", True)

    plan = _make_plan([milestone], task_summary="Research UK housing systems")
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
        )
    )

    assert success is True
    assert len(tools_called) == 1
    assert tools_called[0][0] == "web_search"
    assert "source notes" in result.lower() or "housing system" in result.lower()


def test_execute_milestone_blocks_repeated_failed_search_results():
    responses = [
        json.dumps({
            "done": False,
            "tool": "get_web_information",
            "args": {"query": "funny video", "target_type": "search_results"},
            "reasoning": "Find a video result first",
            "description": "Search for funny videos",
        }),
        json.dumps({
            "done": False,
            "tool": "get_web_information",
            "args": {"query": "funny video", "target_type": "search_results"},
            "reasoning": "Try the same search again",
            "description": "Retry the search",
        }),
        json.dumps({
            "done": False,
            "tool": "open_url",
            "args": {"url": "https://www.youtube.com/results?search_query=funny+video"},
            "reasoning": "Open YouTube results directly instead",
            "description": "Open YouTube search results",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "The browser is on YouTube search results",
            "deliverable": "Opened YouTube search results for funny video",
        }),
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(
        id=1,
        goal="Open a funny video in the browser",
        success_signal="YouTube search results for funny video are open",
        hint_tools=["get_web_information", "open_url"],
    )

    calls = []

    async def env_perceiver():
        return {"active_app": "Electron"}

    async def tool_executor(tool, args):
        calls.append((tool, dict(args)))
        if tool == "get_web_information":
            return (
                json.dumps({
                    "ok": False,
                    "message": "No structured items matched on the current page.",
                    "target_type": "search_results",
                    "error_code": "browser_search_no_results",
                    "route": "browser_aci",
                }),
                False,
            )
        if tool == "open_url":
            return ("Opened YouTube search results for funny video", True)
        return ("unexpected", False)

    plan = _make_plan([milestone], task_summary="Open a funny video")
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
        )
    )

    assert success is True
    assert calls == [
        ("get_web_information", {"query": "funny video", "target_type": "search_results"}),
        ("open_url", {"url": "https://www.youtube.com/results?search_query=funny+video"}),
    ]
    assert "youtube search results" in result.lower()


def test_execute_milestone_tool_then_done():
    """LLM calls one tool, then declares done."""
    responses = [
        json.dumps({
            "done": False,
            "tool": "web_search",
            "args": {"query": "test query"},
            "reasoning": "Need to search first",
            "description": "Searching...",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "Search found the data",
            "deliverable": "Found 3 results about test",
        }),
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(id=1, goal="Search for data", success_signal="3+ results found")

    tools_called = []

    async def env_perceiver():
        return {"active_app": "Chrome"}

    async def tool_executor(tool, args):
        tools_called.append((tool, args))
        return ("search results here", True)

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
        )
    )
    assert success is True
    assert len(tools_called) == 1
    assert tools_called[0] == ("web_search", {"query": "test query"})
    assert "3 results" in result


def test_execute_milestone_stalls_on_zero_yield():
    """Milestone stalls after 3 consecutive zero-yield successful actions."""
    # LLM always says "not done yet, call web_search"
    response = json.dumps({
        "done": False,
        "tool": "web_search",
        "args": {"query": "keep searching"},
        "reasoning": "Still looking",
    })
    llm = FakeLLM([response] * 5)
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(id=1, goal="Find something", success_signal="found it")

    call_count = 0

    async def env_perceiver():
        return {}

    async def tool_executor(tool, args):
        nonlocal call_count
        call_count += 1
        return ("partial result", True)

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
        )
    )
    assert call_count == 3  # 3 zero-yield actions trigger stall
    assert success is False  # no substantive result
    assert milestone.actions_taken == 3
    assert "Stalled after 3 actions" in result


def test_detect_stall_warns_after_repeated_search_results():
    executor = MilestoneExecutor(FakeLLM([]), FAKE_TOOL_DECLS)
    actions = [
        MilestoneAction(
            tool="get_web_information",
            args={"query": "uk housing system overview", "target_type": "search_results"},
            result=json.dumps(
                {
                    "target_type": "search_results",
                    "item_count": 3,
                    "items": [
                        {"label": "Housing overview", "href": "https://example.com/housing"},
                    ],
                }
            ),
            success=True,
        ),
        MilestoneAction(
            tool="get_web_information",
            args={"query": "uk housing tenure overview", "target_type": "search_results"},
            result=json.dumps(
                {
                    "target_type": "search_results",
                    "item_count": 2,
                    "items": [
                        {"label": "Tenure statistics", "href": "https://example.com/tenure"},
                    ],
                }
            ),
            success=True,
        ),
    ]

    ok, warning = executor._detect_stall(actions)
    assert ok is True
    assert "open/read one of those sources next" in warning.lower()


def test_detect_stall_stops_repeated_same_source_reads():
    executor = MilestoneExecutor(FakeLLM([]), FAKE_TOOL_DECLS)
    actions = [
        MilestoneAction(
            tool="get_web_information",
            args={"target_type": "page_summary", "url": "https://example.com/source"},
            result=json.dumps(
                {
                    "target_type": "page_summary",
                    "url": "https://example.com/source",
                    "summary": "Summary one.",
                    "content_length": 120,
                }
            ),
            success=True,
        ),
        MilestoneAction(
            tool="get_web_information",
            args={"target_type": "page_summary", "url": "https://example.com/source"},
            result=json.dumps(
                {
                    "target_type": "page_summary",
                    "url": "https://example.com/source",
                    "summary": "Summary one repeated.",
                    "content_length": 140,
                }
            ),
            success=True,
        ),
    ]

    ok, warning = executor._detect_stall(actions)
    assert ok is False
    assert "same source" in warning.lower()


def test_completion_rejects_irrelevant_substantive_action_for_research_milestone():
    executor = MilestoneExecutor(FakeLLM([]), FAKE_TOOL_DECLS)
    milestone = Milestone(
        id=1,
        goal="Research UK housing systems",
        success_signal="Source notes collected",
        hint_tools=["get_web_information"],
    )
    actions = [
        MilestoneAction(
            tool="gdocs_create",
            args={"title": "UK Housing"},
            result=json.dumps(
                {
                    "ok": True,
                    "url": "https://docs.google.com/document/d/abc",
                    "body_length": 1500,
                    "note": "created",
                }
            ),
            success=True,
        )
    ]

    ok, reason = executor._completion_has_evidence(
        milestone=milestone,
        actions=actions,
        deliverable="Google Doc created with research findings",
    )
    assert ok is False
    assert "evidence" in reason.lower() or "insufficient" in reason.lower()


def test_execute_milestone_rejects_out_of_scope_tool_selection():
    responses = [
        json.dumps({
            "done": False,
            "tool": "gdocs_create",
            "args": {"title": "Premature doc"},
            "reasoning": "I should write the document now",
            "description": "Create document",
        }),
        json.dumps({
            "done": False,
            "tool": "get_web_information",
            "args": {"target_type": "page_summary", "url": "https://example.com/housing"},
            "reasoning": "Read a source first",
            "description": "Read source",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "Collected source notes",
            "deliverable": "Collected source notes about UK housing systems",
        }),
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(
        id=1,
        goal="Research UK housing systems",
        success_signal="Source notes collected",
        hint_tools=["get_web_information"],
    )

    calls = []

    async def env_perceiver():
        return {"active_app": "Chrome"}

    async def tool_executor(tool, args):
        calls.append((tool, args))
        return (
            json.dumps(
                {
                    "target_type": "page_summary",
                    "url": "https://example.com/housing",
                    "summary": "UK housing includes ownership, private renting, and social housing.",
                    "headings": [{"text": "Tenures", "tag": "h2", "ref_id": ""}],
                    "content_length": 120,
                }
            ),
            True,
        )

    plan = _make_plan([milestone], task_summary="Research UK housing systems and write a document")
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
            request_scope_tool_names={"get_web_information", "gdocs_create"},
        )
    )

    assert success is True
    assert calls == [("get_web_information", {"target_type": "page_summary", "url": "https://example.com/housing"})]
    assert "source notes" in result.lower()


def test_execute_milestone_partial_success_with_substantive_result():
    """Stalled milestone can still return partial success when a substantive result exists."""
    responses = [
        json.dumps({
            "done": False,
            "tool": "extract_structured_data",
            "args": {"item_type": "results"},
            "reasoning": "Collect data first",
        }),
        "not-json",
        "not-json",
        "not-json",
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(id=1, goal="Collect data", success_signal="Have extracted records")

    async def env_perceiver():
        return {}

    async def tool_executor(tool, args):
        return ('{"items": ["a", "b", "c"], "item_count": 3, "notes": "' + ("x" * 180) + '"}', True)

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
        )
    )
    assert success is True
    assert "Stalled after" in result


def test_completion_rejects_trivial_keyboard_progress_without_evidence():
    responses = [
        json.dumps({
            "done": False,
            "tool": "press_key",
            "args": {"key": "down"},
            "reasoning": "Try moving focus in the chat list",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "The chat should be focused now",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "The chat should be focused now",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "The chat should be focused now",
        }),
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(
        llm,
        FAKE_TOOL_DECLS + [
            {
                "name": "press_key",
                "description": "Press a raw key",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            }
        ],
    )
    milestone = Milestone(
        id=1,
        goal="Open the Kris chat in WhatsApp",
        success_signal="The Kris chat is focused",
        hint_tools=["press_key"],
    )

    async def env_perceiver():
        return {"active_app": "WhatsApp"}

    async def tool_executor(tool, args):
        assert tool == "press_key"
        return ("Pressed 'down' 1 time(s)", True)

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
            request_scope_tool_names={"press_key"},
        )
    )
    assert success is False
    assert "insufficient evidence" in result.lower() or "stalled" in result.lower()


def test_execute_milestone_suspends_on_await_reply():
    responses = [
        json.dumps({
            "done": False,
            "tool": "await_reply",
            "args": {"message": "Please paste the Luma link"},
            "reasoning": "Need the source link before continuing",
            "description": "Ask for the link",
        })
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(
        llm,
        FAKE_TOOL_DECLS + [
            {
                "name": "await_reply",
                "description": "Ask the user a blocking question",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            }
        ],
    )
    milestone = Milestone(id=1, goal="Find the source link", success_signal="Source link is available")

    async def env_perceiver():
        return {"active_app": "WhatsApp"}

    async def tool_executor(tool, args):
        assert tool == "await_reply"
        return ("Please paste the Luma link", True)

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
            request_scope_tool_names={"await_reply"},
        )
    )
    assert success is False
    assert result.startswith("AWAIT_REPLY:")


def test_execute_milestone_blocks_repeated_await_reply_signature():
    responses = [
        json.dumps({
            "done": False,
            "tool": "await_reply",
            "args": {"message": "Please paste the Luma link"},
            "reasoning": "Trying the same question again",
            "description": "Ask for the link again",
        }),
        json.dumps({
            "done": False,
            "tool": "get_web_information",
            "args": {"target_type": "page_summary", "url": "https://lu.ma/encode-demo"},
            "reasoning": "Use the link already provided",
            "description": "Read the demo page",
        }),
        json.dumps({
            "done": True,
            "tool": "",
            "args": {},
            "reasoning": "Collected the demo tasks",
            "deliverable": "Collected the demo tasks",
        }),
    ]
    llm = FakeLLM(responses)
    executor = MilestoneExecutor(
        llm,
        FAKE_TOOL_DECLS + [
            {
                "name": "await_reply",
                "description": "Ask the user a blocking question",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            }
        ],
    )
    milestone = Milestone(
        id=1,
        goal="Retrieve the encode demo tasks",
        success_signal="Demo tasks are collected",
        hint_tools=["await_reply", "get_web_information"],
    )

    calls = []

    async def env_perceiver():
        return {"active_app": "WhatsApp", "user_followups": "https://lu.ma/encode-demo"}

    async def tool_executor(tool, args):
        calls.append((tool, args))
        return (
            json.dumps({
                "target_type": "page_summary",
                "url": "https://lu.ma/encode-demo",
                "summary": "Arrive early, introduce the demo, and handle Q&A.",
                "content_length": 120,
            }),
            True,
        )

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
            request_scope_tool_names={"await_reply", "get_web_information"},
            blocked_await_signatures={'{"message":"Please paste the Luma link"}'},
        )
    )
    assert success is True
    assert "demo tasks" in result.lower()
    assert calls == [("get_web_information", {"target_type": "page_summary", "url": "https://lu.ma/encode-demo"})]


def test_execute_milestone_all_failures():
    """All tool calls fail → milestone fails."""
    response = json.dumps({
        "done": False,
        "tool": "web_search",
        "args": {"query": "fail"},
        "reasoning": "Trying",
    })
    llm = FakeLLM([response] * 60)
    executor = MilestoneExecutor(llm, FAKE_TOOL_DECLS)
    milestone = Milestone(id=1, goal="Do something", success_signal="done")

    async def env_perceiver():
        return {}

    async def tool_executor(tool, args):
        return ("ERROR: connection failed", False)

    plan = _make_plan([milestone])
    success, result = asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables={},
        )
    )
    assert success is False
    assert milestone.actions_taken == 50


def test_execute_milestone_deliverables_passed():
    """Previous milestone deliverables are available in the prompt."""
    # Check that the LLM receives deliverable context by inspecting the prompt
    captured_prompts = []

    class CaptureLLM:
        name = "capture"

        async def generate(self, messages, system_prompt="", tools=None, temperature=0.0):
            captured_prompts.append(messages[0]["parts"][0]["text"])
            return FakeResponse('{"done": true, "tool": "", "args": {}, "reasoning": "got it", "deliverable": "ok"}')

    executor = MilestoneExecutor(CaptureLLM(), FAKE_TOOL_DECLS)
    milestone = Milestone(id=2, goal="Use data", success_signal="data used", depends_on=[1])

    prev_deliverables = {1: "research data from milestone 1"}

    async def env_perceiver():
        return {"active_app": "Finder"}

    async def tool_executor(tool, args):
        return ("ok", True)

    plan = _make_plan([
        Milestone(id=1, goal="Collect data", success_signal="data collected"),
        milestone,
    ])
    asyncio.run(
        executor.execute_milestone(
            milestone=milestone,
            plan=plan,
            env_perceiver=env_perceiver,
            tool_executor=tool_executor,
            deliverables=prev_deliverables,
        )
    )
    assert len(captured_prompts) == 1
    assert "research data from milestone 1" in captured_prompts[0]


def test_milestone_action_dataclass():
    """MilestoneAction stores action metadata correctly."""
    a = MilestoneAction(
        tool="web_search",
        args={"query": "test"},
        result="found it",
        success=True,
        duration=0.5,
    )
    assert a.tool == "web_search"
    assert a.success is True
    assert a.duration == 0.5


def test_prompts_have_placeholders():
    """Prompt templates contain required format placeholders."""
    required_keys = [
        "task_summary", "milestone_id", "total_milestones",
        "milestone_goal", "success_signal", "hint_tools",
        "deliverables", "search_leads", "action_count", "action_history",
        "stall_warning", "env_state",
        "last_result", "tool_list",
    ]
    for key in required_keys:
        assert f"{{{key}}}" in MILESTONE_EXECUTOR_PROMPT, f"Missing placeholder: {key}"

    assert "CRITICAL RULES" in MILESTONE_EXECUTOR_SYSTEM
    assert "success_signal" in MILESTONE_EXECUTOR_SYSTEM
