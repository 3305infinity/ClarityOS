from __future__ import annotations

import asyncio

from experiments.macos_agents.bench import harness
from experiments.macos_agents.models import ComparisonRow, FailureRecord, RunResult, ScenarioDefinition, ToolTrace


class _FakeAgent:
    def __init__(self, agent_name: str, artifact_root):
        self.agent_name = agent_name
        self.artifact_root = artifact_root
        self.calls: list[dict] = []

    async def run(self, task: str, *, scenario=None, run_mode: str = "live", seed_context=None):
        self.calls.append(
            {
                "task": task,
                "scenario": scenario,
                "run_mode": run_mode,
                "seed_context": seed_context,
            }
        )
        mode = "vision" if self.agent_name == "vision_first" else "ax"
        return RunResult(
            agent_name=self.agent_name,
            scenario_name=scenario.name,
            success=self.agent_name != "hybrid_router",
            final_state_summary=f"{self.agent_name} finished {scenario.name}",
            latency_ms=120 if self.agent_name == "ax_first" else 240,
            step_count=2,
            tool_calls=[ToolTrace(name="finish_run", args={}, duration_ms=1, ok=True, output="{}", mode=mode)],
            failures=[] if self.agent_name != "hybrid_router" else [FailureRecord(stage="agent", reason="blocked")],
            artifacts={"run_result": str(self.artifact_root / "run_result.json")},
            metadata={"run_mode": run_mode},
        )


def test_run_agent_scenario_merges_seed_context(monkeypatch, tmp_path) -> None:
    created: list[_FakeAgent] = []

    def fake_create_agent(agent_name, provider, artifact_root=None):
        agent = _FakeAgent(agent_name, artifact_root)
        created.append(agent)
        return agent

    monkeypatch.setattr(harness, "create_agent", fake_create_agent)
    scenario = ScenarioDefinition(
        "open_notes",
        "Open Notes.",
        [],
        ["Notes is frontmost."],
        seed_context={"target_app": "Notes"},
    )

    result = asyncio.run(
        harness.run_agent_scenario(
            "ax_first",
            scenario,
            provider=object(),
            run_mode="dry",
            artifact_root=tmp_path,
            seed_context={"user_hint": "dock"},
        )
    )

    assert result.agent_name == "ax_first"
    assert set(result.as_dict().keys()) >= {"agent_name", "scenario_name", "success", "tool_calls", "artifacts"}
    assert created[0].calls[0]["seed_context"] == {"target_app": "Notes", "user_hint": "dock"}


def test_compare_agent_set_aggregates_metrics(monkeypatch, tmp_path) -> None:
    scenario_a = ScenarioDefinition("open_notes", "Open Notes.", [], ["Notes is frontmost."])
    scenario_b = ScenarioDefinition("search_field_confirm", "Search.", [], ["Search completes."])

    monkeypatch.setattr(harness, "get_scenario_set", lambda name: [scenario_a, scenario_b])
    monkeypatch.setattr(harness, "create_agent", lambda agent_name, provider, artifact_root=None: _FakeAgent(agent_name, artifact_root))

    summary = asyncio.run(
        harness.compare_agent_set(
            "core_desktop",
            ["ax_first", "vision_first", "hybrid_router"],
            provider=object(),
            run_mode="dry",
            artifact_root=tmp_path,
        )
    )

    assert len(summary.rows) == 6
    assert len(summary.agent_metrics) == 3
    metrics = {metric.agent_name: metric for metric in summary.agent_metrics}
    assert metrics["ax_first"].success_rate == 1.0
    assert metrics["vision_first"].average_fallback_count == 1.0
    assert metrics["hybrid_router"].success_rate == 0.0
    assert any(path.endswith("run_result.json") for path in summary.generated_files)
