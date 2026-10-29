from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ToolExecutionResult:
    ok: bool
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    terminal: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = {"ok": self.ok, "message": self.message}
        if self.payload:
            data.update(self.payload)
        return data


@dataclass
class ToolTrace:
    name: str
    args: dict[str, Any]
    duration_ms: int
    ok: bool
    output: str
    mode: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": self.args,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "output": self.output,
            "mode": self.mode,
        }


@dataclass
class FailureRecord:
    stage: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass
class ScenarioDefinition:
    name: str
    task: str
    preconditions: list[str]
    success_checks: list[str]
    max_steps: int = 8
    timeout_s: int = 90
    seed_context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "task": self.task,
            "preconditions": self.preconditions,
            "success_checks": self.success_checks,
            "max_steps": self.max_steps,
            "timeout_s": self.timeout_s,
            "seed_context": self.seed_context,
        }


@dataclass
class RunResult:
    agent_name: str
    scenario_name: str
    success: bool
    final_state_summary: str
    latency_ms: int
    step_count: int
    tool_calls: list[ToolTrace] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    failure_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "scenario_name": self.scenario_name,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "final_state_summary": self.final_state_summary,
            "latency_ms": self.latency_ms,
            "step_count": self.step_count,
            "tool_calls": [trace.as_dict() for trace in self.tool_calls],
            "failures": [failure.as_dict() for failure in self.failures],
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }


@dataclass
class ToolRuntime:
    run_mode: str
    artifacts_dir: Path
    llm_provider: Any = None
    state: dict[str, Any] = field(default_factory=dict)

    def remember_artifact(self, key: str, value: str) -> None:
        self.state.setdefault("artifacts", {})
        self.state["artifacts"][key] = value

    def artifact_map(self) -> dict[str, str]:
        artifacts = self.state.get("artifacts", {})
        return {str(k): str(v) for k, v in artifacts.items()}


@dataclass
class ComparisonRow:
    agent_name: str
    scenario_name: str
    success: bool
    latency_ms: int
    fallback_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "scenario_name": self.scenario_name,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "fallback_count": self.fallback_count,
        }


@dataclass
class ComparisonAggregate:
    agent_name: str
    run_count: int
    success_count: int
    success_rate: float
    average_latency_ms: int
    average_fallback_count: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "run_count": self.run_count,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
            "average_latency_ms": self.average_latency_ms,
            "average_fallback_count": self.average_fallback_count,
        }


@dataclass
class ComparisonSummary:
    scenario_set: str
    rows: list[ComparisonRow]
    agent_metrics: list[ComparisonAggregate] = field(default_factory=list)
    generated_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_set": self.scenario_set,
            "rows": [row.as_dict() for row in self.rows],
            "agent_metrics": [metric.as_dict() for metric in self.agent_metrics],
            "generated_files": self.generated_files,
        }


class AgentExecutionError(RuntimeError):
    """Raised when an experiment-local agent cannot complete a run."""


class ExperimentConfigurationError(RuntimeError):
    """Raised when experiment-local configuration is invalid."""
