from __future__ import annotations

import time

from typing import Optional

from ..models import FailureRecord, RunResult, ScenarioDefinition, ToolRuntime, ToolTrace
from ..toolbox import ExperimentToolbox
from ..tools import build_ax_tools, build_low_level_tools, build_vision_tools
from ..utils import new_artifact_dir, parse_json_object, shorten, write_json
from .base import BaseExperimentAgent


ROUTER_PROMPT = """You are the routing controller for a macOS experiment agent.
Choose exactly one interaction mode for the next step and return compact JSON:
{"mode":"ax|vision|low_level","reason":"..."}
Prefer:
- ax: structured apps with likely Accessibility exposure
- vision: visually rich surfaces or unknown structure
- low_level: only after repeated misses/timeouts or for direct shortcut/scroll work
If the same mode has failed twice, switch away from it."""


class HybridRouterAgent(BaseExperimentAgent):
    agent_name = "hybrid_router"
    system_prompt = """You are the Hybrid Router worker for a macOS experiment agent.
You will be told which mode to use for the next step.
Use only the tools exposed for that mode.
If you cannot make progress, use finish_run with success=false and explain what blocked the run."""

    def build_toolbox(self, runtime):
        return ExperimentToolbox(build_ax_tools() + build_vision_tools() + build_low_level_tools())

    def _mode_tools(self) -> dict[str, set[str]]:
        return {
            "ax": {"activate_app", "get_ui_tree", "semantic_click", "focus_and_type", "type_text", "press_key", "run_shortcut", "finish_run"},
            "vision": {"activate_app", "vision_read_screen", "vision_ground_element", "click_point", "type_text", "press_key", "run_shortcut", "scroll_view", "finish_run"},
            "low_level": {"activate_app", "type_text", "press_key", "run_shortcut", "click_point", "move_mouse", "drag_mouse", "scroll_view", "finish_run"},
        }

    def _forced_mode(self, runtime: ToolRuntime, selected_mode: str) -> str:
        failures = runtime.state.setdefault("mode_failures", {})
        if failures.get(selected_mode, 0) < 2:
            return selected_mode
        fallback_map = {"ax": "vision", "vision": "low_level", "low_level": "ax"}
        return fallback_map.get(selected_mode, "low_level")

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
            state={"seed_context": dict(seed_context or {}), "mode_failures": {}, "mode_history": []},
        )
        toolbox = self.build_toolbox(runtime)
        worker_messages = [
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
        mode_tools = self._mode_tools()

        for step_index in range(1, scenario_def.max_steps + 1):
            router_context = {
                "task": task,
                "scenario": scenario_def.as_dict(),
                "recent_failures": [failure.as_dict() for failure in failures[-4:]],
                "mode_failures": runtime.state.get("mode_failures", {}),
                "last_mode": runtime.state.get("current_mode", ""),
                "last_typed_text": runtime.state.get("last_typed_text", ""),
            }
            router_response = await self.provider.generate(
                messages=[{"role": "user", "parts": [{"text": str(router_context)}]}],
                system_prompt=ROUTER_PROMPT,
                tools=[],
                temperature=0.0,
            )
            if router_response.error:
                failure_reason = router_response.error
                failures.append(FailureRecord(stage="router", reason=router_response.error))
                break
            router_data = parse_json_object(router_response.text or "")
            selected_mode = str(router_data.get("mode", "ax") or "ax").strip().lower()
            if selected_mode not in mode_tools:
                selected_mode = "ax"
            selected_mode = self._forced_mode(runtime, selected_mode)
            runtime.state["current_mode"] = selected_mode
            runtime.state.setdefault("mode_history", []).append(selected_mode)

            allowed_tools = mode_tools[selected_mode]
            worker_messages.append(
                {
                    "role": "user",
                    "parts": [{"text": f"Mode for next step: {selected_mode}\nReason: {router_data.get('reason', '')}"}],
                }
            )
            response = await self._generate(worker_messages, toolbox, allowed_tools=allowed_tools)
            if response.error:
                failure_reason = response.error
                failures.append(FailureRecord(stage="worker", reason=response.error, details={"mode": selected_mode}))
                runtime.state["mode_failures"][selected_mode] = runtime.state["mode_failures"].get(selected_mode, 0) + 1
                break
            if response.raw_model_parts:
                worker_messages.append({"role": "model", "parts": response.raw_model_parts})
            if not response.has_tool_calls:
                failure_reason = shorten(response.text or "Worker returned no tool calls")
                failures.append(FailureRecord(stage="worker", reason=failure_reason, details={"mode": selected_mode}))
                runtime.state["mode_failures"][selected_mode] = runtime.state["mode_failures"].get(selected_mode, 0) + 1
                break

            tool_responses: list[dict] = []
            step_failed = False
            for tool_call in response.tool_calls:
                t0 = time.time()
                tool_result = await toolbox.execute(tool_call.name, tool_call.args, runtime)
                encoded = ExperimentToolbox.encode_for_model(tool_result)
                traces.append(
                    ToolTrace(
                        name=tool_call.name,
                        args=tool_call.args,
                        duration_ms=int((time.time() - t0) * 1000),
                        ok=tool_result.ok,
                        output=encoded,
                        mode=selected_mode,
                    )
                )
                if not tool_result.ok:
                    step_failed = True
                    failures.append(FailureRecord(stage=tool_call.name, reason=tool_result.message, details={"mode": selected_mode, **tool_call.args}))
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
                            "mode_history": runtime.state.get("mode_history", []),
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

            if step_failed:
                runtime.state["mode_failures"][selected_mode] = runtime.state["mode_failures"].get(selected_mode, 0) + 1
            else:
                runtime.state["mode_failures"][selected_mode] = 0

            if tool_responses:
                worker_messages.append({"role": "user", "parts": tool_responses})

        result = RunResult(
            agent_name=self.agent_name,
            scenario_name=scenario_def.name,
            success=False,
            final_state_summary=final_summary or "Hybrid run did not complete successfully.",
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
                "mode_history": runtime.state.get("mode_history", []),
            },
        )
        write_json(artifacts_dir / "run_result.json", result.as_dict())
        return result
