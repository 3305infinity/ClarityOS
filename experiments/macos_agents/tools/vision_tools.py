from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..models import ToolExecutionResult, ToolRuntime
from ..shared_provider import LLMProvider
from ..toolbox import ExperimentTool
from ..utils import parse_json_object, shorten, write_text
from .common import capture_screenshot, make_image_digest, tool_failure, tool_success


VISION_READ_PROMPT = """You are reading a macOS screenshot for an experiment harness.
Answer in compact JSON with keys:
- summary: short description of the visible UI
- visible_text: important text snippets
- clickable_targets: short list of obvious actionable targets
Do not include markdown."""

VISION_GROUND_PROMPT = """You are locating a macOS UI target in a screenshot for an experiment harness.
Return compact JSON only with keys:
- target
- x
- y
- confidence
- rationale
If the target is not visible, set confidence to 0 and explain why."""


async def _vision_provider(runtime: ToolRuntime) -> Optional[LLMProvider]:
    provider = runtime.llm_provider
    return provider if provider is not None else None


async def _vision_read_screen(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    question = str(args.get("question", "") or "Describe the visible UI state.").strip()
    screenshot_path, status = await capture_screenshot(runtime, prefix="vision_screen")
    if screenshot_path is None:
        return tool_failure(status)
    if runtime.run_mode == "dry":
        payload = {
            "summary": "[dry-run] UI with search field and primary action button",
            "visible_text": ["Search", "Open", "Cancel"],
            "clickable_targets": ["Search", "Open"],
            "question": question,
        }
        transcript_path = screenshot_path.with_suffix(".vision.json")
        write_text(transcript_path, __import__("json").dumps(payload, indent=2))
        runtime.remember_artifact("vision_read", str(transcript_path))
        return tool_success("Vision read complete", screenshot_path=str(screenshot_path), response=payload)

    provider = await _vision_provider(runtime)
    if provider is None:
        return tool_failure("No Gemini provider available for vision_read_screen")
    response = await provider.generate(
        messages=[{"role": "user", "parts": [{"text": question}]}],
        system_prompt=VISION_READ_PROMPT,
        tools=[],
        image_data=screenshot_path.read_bytes(),
        temperature=0.1,
    )
    if response.error:
        return tool_failure(response.error)
    payload = parse_json_object(response.text or "")
    if not payload:
        payload = {"summary": shorten(response.text or "No vision summary returned"), "visible_text": [], "clickable_targets": []}
    payload.setdefault("image_digest", make_image_digest(screenshot_path))
    transcript_path = screenshot_path.with_suffix(".vision.json")
    write_text(transcript_path, __import__("json").dumps(payload, indent=2))
    runtime.remember_artifact("vision_read", str(transcript_path))
    return tool_success("Vision read complete", screenshot_path=str(screenshot_path), response=payload)


async def _vision_ground_element(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    target = str(args.get("target_description", "") or "").strip()
    if not target:
        return tool_failure("target_description is required")
    screenshot_path, status = await capture_screenshot(runtime, prefix="vision_ground")
    if screenshot_path is None:
        return tool_failure(status)
    if runtime.run_mode == "dry":
        payload = {"target": target, "x": 320, "y": 180, "confidence": 0.82, "rationale": "[dry-run] simulated grounding"}
        transcript_path = screenshot_path.with_suffix(".ground.json")
        write_text(transcript_path, __import__("json").dumps(payload, indent=2))
        runtime.remember_artifact("vision_ground", str(transcript_path))
        return tool_success("Vision grounding complete", screenshot_path=str(screenshot_path), grounding=payload)

    provider = await _vision_provider(runtime)
    if provider is None:
        return tool_failure("No Gemini provider available for vision_ground_element")
    response = await provider.generate(
        messages=[{"role": "user", "parts": [{"text": f"Target: {target}"}]}],
        system_prompt=VISION_GROUND_PROMPT,
        tools=[],
        image_data=screenshot_path.read_bytes(),
        temperature=0.0,
    )
    if response.error:
        return tool_failure(response.error)
    payload = parse_json_object(response.text or "")
    if not payload:
        return tool_failure(f"Could not parse vision grounding response: {shorten(response.text or '')}")
    transcript_path = screenshot_path.with_suffix(".ground.json")
    write_text(transcript_path, __import__("json").dumps(payload, indent=2))
    runtime.remember_artifact("vision_ground", str(transcript_path))
    return tool_success(
        f"Grounded target '{target}'",
        screenshot_path=str(screenshot_path),
        grounding=payload,
    )


def build_vision_tools() -> list[ExperimentTool]:
    return [
        ExperimentTool(
            name="vision_read_screen",
            description="Capture a screenshot and summarize the visible macOS UI using Gemini vision.",
            parameters={
                "type": "object",
                "properties": {"question": {"type": "string"}},
            },
            func=_vision_read_screen,
        ),
        ExperimentTool(
            name="vision_ground_element",
            description="Capture a screenshot and locate a UI target, returning x/y coordinates and confidence.",
            parameters={
                "type": "object",
                "properties": {"target_description": {"type": "string"}},
                "required": ["target_description"],
            },
            func=_vision_ground_element,
        ),
    ]
