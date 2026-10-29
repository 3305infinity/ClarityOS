from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from ..models import FailureRecord, RunResult, ScenarioDefinition, ToolRuntime, ToolTrace
from ..shared_provider import LLMProvider, LLMResponse
from ..toolbox import ExperimentToolbox
from ..utils import json_dumps, new_artifact_dir, shorten, write_json


ToolboxFactory = Callable[[ToolRuntime], ExperimentToolbox]


class BaseExperimentAgent:
    agent_name = "base_agent"
    system_prompt = ""

    def __init__(
        self,
        provider: LLMProvider,
        artifact_root: Optional[Path] = None,
        toolbox_factory: Optional[ToolboxFactory] = None,
    ) -> None:
        self.provider = provider
        self.artifact_root = artifact_root
        self._toolbox_factory = toolbox_factory

    def build_toolbox(self, runtime: ToolRuntime) -> ExperimentToolbox:
        if self._toolbox_factory is not None:
            return self._toolbox_factory(runtime)
        raise NotImplementedError

    def scenario_prompt(
        self,
        task: str,
        scenario: ScenarioDefinition,
        seed_context: dict,
    ) -> str:
        return (
            f"Task: {task}\n"
            f"Scenario: {scenario.name}\n"
            f"Preconditions: {'; '.join(scenario.preconditions)}\n"
            f"Success checks: {'; '.join(scenario.success_checks)}\n"
            f"Seed context: {json_dumps(seed_context) if seed_context else '{}'}\n"
            "Use the available tools to complete the task. Always call finish_run when the scenario is complete or blocked."
        )

    async def _generate(
        self,
        messages: list[dict],
        toolbox: ExperimentToolbox,
        allowed_tools: Optional[set[str]] = None,
    ) -> LLMResponse:
        return await self.provider.generate(
            messages=messages,
            system_prompt=self.system_prompt,
            tools=toolbox.declarations(include=allowed_tools),
            temperature=0.15,
        )

    async def run(
        self,
        task: str,
        *,
        scenario: Optional[ScenarioDefinition] = None,
        run_mode: str = "live",
        seed_context: Optional[dict] = None,
    ) -> RunResult:
        scenario_def = scenario or ScenarioDefinition(
            name="adhoc_task",
            task=task,
            preconditions=[],
            success_checks=["Agent reports the task is complete."],
            max_steps=8,
            timeout_s=90,
        )
        artifacts_dir = self.artifact_root or new_artifact_dir(f"{self.agent_name}_{scenario_def.name}")
        runtime = ToolRuntime(
            run_mode=run_mode,
            artifacts_dir=artifacts_dir,
            llm_provider=self.provider,
            state={"seed_context": dict(seed_context or {})},
        )
        toolbox = self.build_toolbox(runtime)
        messages: list[dict] = [
            {
                "role": "user",
                "parts": [{"text": self.scenario_prompt(task, scenario_def, dict(seed_context or {}))}],
            }
        ]
        traces: list[ToolTrace] = []
        failures: list[FailureRecord] = []
        started = time.time()
        final_summary = ""
        failure_reason = ""

        for step_index in range(1, scenario_def.max_steps + 1):
            response = await self._generate(messages, toolbox)
            if response.error:
                failure_reason = response.error
                failures.append(FailureRecord(stage="llm", reason=response.error))
                break
            if response.raw_model_parts:
                messages.append({"role": "model", "parts": response.raw_model_parts})
            if not response.has_tool_calls:
                failure_reason = shorten(response.text or "LLM returned no tool calls")
                failures.append(FailureRecord(stage="llm", reason=failure_reason))
                break

            tool_responses: list[dict] = []
            for tool_call in response.tool_calls:
                t0 = time.time()
                tool_result = await toolbox.execute(tool_call.name, tool_call.args, runtime)
                duration_ms = int((time.time() - t0) * 1000)
                encoded = ExperimentToolbox.encode_for_model(tool_result)
                traces.append(
                    ToolTrace(
                        name=tool_call.name,
                        args=tool_call.args,
                        duration_ms=duration_ms,
                        ok=tool_result.ok,
                        output=encoded,
                        mode=str(runtime.state.get("current_mode", "")),
                    )
                )
                if not tool_result.ok:
                    failures.append(FailureRecord(stage=tool_call.name, reason=tool_result.message, details=tool_call.args))
                if tool_result.terminal:
                    final_summary = str(tool_result.payload.get("summary", tool_result.message) or tool_result.message)
                    success = bool(tool_result.payload.get("success", tool_result.ok))
                    failure_reason = str(tool_result.payload.get("failure_reason", "") or "")
                    result = RunResult(
                        agent_name=self.agent_name,
                        scenario_name=scenario_def.name,
                        success=success,
                        final_state_summary=final_summary,
                        failure_reason=failure_reason,
                        latency_ms=int((time.time() - started) * 1000),
                        step_count=step_index,
                        tool_calls=traces,
                        failures=failures,
                        artifacts=runtime.artifact_map(),
                        metadata={
                            "run_mode": run_mode,
                            "provider": self.provider.name,
                            "scenario": scenario_def.as_dict(),
                        },
                    )
                    write_json(artifacts_dir / "run_result.json", result.as_dict())
                    return result
                tool_responses.append(
                    {
                        "function_response": {
                            "name": tool_call.name,
                            "response": {"result": encoded},
                        }
                    }
                )
            if tool_responses:
                messages.append({"role": "user", "parts": tool_responses})

        result = RunResult(
            agent_name=self.agent_name,
            scenario_name=scenario_def.name,
            success=False,
            final_state_summary=final_summary or "Run did not complete successfully.",
            failure_reason=failure_reason or "step budget exceeded",
            latency_ms=int((time.time() - started) * 1000),
            step_count=len(traces),
            tool_calls=traces,
            failures=failures or [FailureRecord(stage="agent", reason=failure_reason or "step budget exceeded")],
            artifacts=runtime.artifact_map(),
            metadata={
                "run_mode": run_mode,
                "provider": self.provider.name,
                "scenario": scenario_def.as_dict(),
            },
        )
        write_json(artifacts_dir / "run_result.json", result.as_dict())
        return result
