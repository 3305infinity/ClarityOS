from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from ..architectures import create_agent
from ..models import ComparisonAggregate, ComparisonRow, ComparisonSummary, RunResult, ScenarioDefinition
from ..provider_factory import load_gemini_provider
from ..shared_provider import LLMProvider
from ..scenarios import get_scenario_set
from ..utils import default_artifact_root, ensure_directory, merge_seed_context, new_artifact_dir, write_json


async def run_agent_scenario(
    agent_name: str,
    scenario: ScenarioDefinition,
    *,
    provider: Optional[LLMProvider] = None,
    run_mode: str = "live",
    artifact_root: Optional[Path] = None,
    seed_context: Optional[dict] = None,
) -> RunResult:
    resolved_provider = provider or load_gemini_provider()
    resolved_artifact_root = artifact_root or new_artifact_dir(f"{agent_name}_{scenario.name}")
    agent = create_agent(agent_name, resolved_provider, artifact_root=resolved_artifact_root)
    return await agent.run(
        scenario.task,
        scenario=scenario,
        run_mode=run_mode,
        seed_context=merge_seed_context(scenario.seed_context, seed_context),
    )


async def compare_agent_set(
    scenario_set_name: str,
    agent_names: Iterable[str],
    *,
    provider: Optional[LLMProvider] = None,
    run_mode: str = "live",
    artifact_root: Optional[Path] = None,
) -> ComparisonSummary:
    resolved_provider = provider or load_gemini_provider()
    scenario_defs = get_scenario_set(scenario_set_name)
    out_root = ensure_directory(artifact_root or new_artifact_dir(f"compare_{scenario_set_name}"))
    rows: list[ComparisonRow] = []
    generated_files: list[str] = []

    for scenario in scenario_defs:
        for agent_name in agent_names:
            run_root = ensure_directory(out_root / scenario.name / agent_name)
            result = await run_agent_scenario(
                agent_name,
                scenario,
                provider=resolved_provider,
                run_mode=run_mode,
                artifact_root=run_root,
            )
            fallback_count = sum(1 for trace in result.tool_calls if trace.mode in {"vision", "low_level"} and trace.ok)
            rows.append(
                ComparisonRow(
                    agent_name=agent_name,
                    scenario_name=scenario.name,
                    success=result.success,
                    latency_ms=result.latency_ms,
                    fallback_count=fallback_count,
                )
            )
            generated_files.append(str(run_root / "run_result.json"))

    agent_metrics: list[ComparisonAggregate] = []
    grouped_rows: dict[str, list[ComparisonRow]] = {}
    for row in rows:
        grouped_rows.setdefault(row.agent_name, []).append(row)
    for agent_name, agent_rows in grouped_rows.items():
        run_count = len(agent_rows)
        success_count = sum(1 for row in agent_rows if row.success)
        total_latency = sum(row.latency_ms for row in agent_rows)
        total_fallbacks = sum(row.fallback_count for row in agent_rows)
        agent_metrics.append(
            ComparisonAggregate(
                agent_name=agent_name,
                run_count=run_count,
                success_count=success_count,
                success_rate=(success_count / run_count) if run_count else 0.0,
                average_latency_ms=int(total_latency / run_count) if run_count else 0,
                average_fallback_count=(total_fallbacks / run_count) if run_count else 0.0,
            )
        )
    agent_metrics.sort(key=lambda metric: metric.agent_name)

    summary = ComparisonSummary(
        scenario_set=scenario_set_name,
        rows=rows,
        agent_metrics=agent_metrics,
        generated_files=generated_files,
    )
    write_json(out_root / "comparison_summary.json", summary.as_dict())
    return summary
