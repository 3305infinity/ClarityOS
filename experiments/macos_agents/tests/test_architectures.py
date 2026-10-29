from __future__ import annotations

import asyncio
from typing import Optional

from experiments.macos_agents.architectures.ax_first import AXFirstAgent
from experiments.macos_agents.architectures.hybrid_router import HybridRouterAgent
from experiments.macos_agents.architectures.vision_first import VisionFirstAgent
from experiments.macos_agents.models import ScenarioDefinition, ToolExecutionResult
from experiments.macos_agents.shared_provider import LLMResponse, ToolCall
from experiments.macos_agents.tests.fakes import ScriptedProvider
from experiments.macos_agents.toolbox import ExperimentTool


def _stub_tool(
    name: str,
    recorder: list[str],
    *,
    ok: bool = True,
    message: str = "ok",
    payload: Optional[dict] = None,
    terminal: bool = False,
) -> ExperimentTool:
    async def _func(args, runtime):
        recorder.append(name)
        return ToolExecutionResult(ok=ok, message=message, payload=payload or {}, terminal=terminal)

    return ExperimentTool(
        name=name,
        description=f"stub {name}",
        parameters={"type": "object", "properties": {}},
        func=_func,
    )


def test_ax_first_succeeds_without_vision(monkeypatch, tmp_path) -> None:
    import experiments.macos_agents.architectures.ax_first as ax_first_module

    recorder: list[str] = []
    monkeypatch.setattr(ax_first_module, "build_ax_tools", lambda: [_stub_tool("get_ui_tree", recorder)])
    monkeypatch.setattr(
        ax_first_module,
        "build_low_level_tools",
        lambda: [_stub_tool("finish_run", recorder, payload={"summary": "AX path completed", "success": True}, terminal=True)],
    )
    provider = ScriptedProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall("get_ui_tree", {"app_name": "Notes"}),
                    ToolCall("finish_run", {"summary": "AX path completed", "success": True}),
                ]
            )
        ]
    )
    scenario = ScenarioDefinition("open_notes", "Open Notes.", [], ["Notes is frontmost."], max_steps=2)
    agent = AXFirstAgent(provider=provider, artifact_root=tmp_path)

    result = asyncio.run(agent.run(scenario.task, scenario=scenario, run_mode="dry"))

    assert result.success is True
    assert recorder == ["get_ui_tree", "finish_run"]
    assert all(trace.name != "vision_read_screen" for trace in result.tool_calls)


def test_vision_first_recovers_after_ax_miss(monkeypatch, tmp_path) -> None:
    import experiments.macos_agents.architectures.vision_first as vision_first_module

    recorder: list[str] = []
    monkeypatch.setattr(
        vision_first_module,
        "build_ax_tools",
        lambda: [_stub_tool("get_ui_tree", recorder, ok=False, message="AX tree empty")],
    )
    monkeypatch.setattr(
        vision_first_module,
        "build_vision_tools",
        lambda: [_stub_tool("vision_read_screen", recorder, payload={"response": {"summary": "Search field visible"}})],
    )
    monkeypatch.setattr(
        vision_first_module,
        "build_low_level_tools",
        lambda: [_stub_tool("finish_run", recorder, payload={"summary": "Vision path completed", "success": True}, terminal=True)],
    )
    provider = ScriptedProvider(
        [
            LLMResponse(tool_calls=[ToolCall("get_ui_tree", {})]),
            LLMResponse(
                tool_calls=[
                    ToolCall("vision_read_screen", {"question": "What is visible?"}),
                    ToolCall("finish_run", {"summary": "Vision path completed", "success": True}),
                ]
            ),
        ]
    )
    scenario = ScenarioDefinition("search_field_confirm", "Find the search field.", [], ["Search is confirmed."], max_steps=3)
    agent = VisionFirstAgent(provider=provider, artifact_root=tmp_path)

    result = asyncio.run(agent.run(scenario.task, scenario=scenario, run_mode="dry"))

    assert result.success is True
    assert recorder == ["get_ui_tree", "vision_read_screen", "finish_run"]
    assert result.failures[0].reason == "AX tree empty"


def test_hybrid_router_switches_modes_after_repeated_failures(monkeypatch, tmp_path) -> None:
    import experiments.macos_agents.architectures.hybrid_router as hybrid_module

    recorder: list[str] = []
    monkeypatch.setattr(
        hybrid_module,
        "build_ax_tools",
        lambda: [_stub_tool("semantic_click", recorder, ok=False, message="AX miss")],
    )
    monkeypatch.setattr(
        hybrid_module,
        "build_vision_tools",
        lambda: [_stub_tool("vision_read_screen", recorder, payload={"response": {"summary": "Visible target"}})],
    )
    monkeypatch.setattr(
        hybrid_module,
        "build_low_level_tools",
        lambda: [_stub_tool("finish_run", recorder, payload={"summary": "Hybrid complete", "success": True}, terminal=True)],
    )

    provider = ScriptedProvider(
        [
            LLMResponse(text='{"mode":"ax","reason":"structured"}'),
            LLMResponse(tool_calls=[ToolCall("semantic_click", {"description": "Kris"})]),
            LLMResponse(text='{"mode":"ax","reason":"structured"}'),
            LLMResponse(tool_calls=[ToolCall("semantic_click", {"description": "Kris"})]),
            LLMResponse(text='{"mode":"ax","reason":"structured"}'),
            LLMResponse(
                tool_calls=[
                    ToolCall("vision_read_screen", {"question": "Find Kris"}),
                    ToolCall("finish_run", {"summary": "Hybrid complete", "success": True}),
                ]
            ),
        ]
    )
    scenario = ScenarioDefinition("whatsapp_message", "Message Kris.", [], ["Kris is messaged."], max_steps=4)
    agent = HybridRouterAgent(provider=provider, artifact_root=tmp_path)

    result = asyncio.run(agent.run(scenario.task, scenario=scenario, run_mode="dry"))

    assert result.success is True
    assert recorder == ["semantic_click", "semantic_click", "vision_read_screen", "finish_run"]
    assert result.metadata["mode_history"] == ["ax", "ax", "vision"]
