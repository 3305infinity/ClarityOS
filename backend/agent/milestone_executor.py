"""
Moonwalk — Milestone Executor
==============================
LLM micro-loop engine for milestone-based execution.

Unlike the step-based executor that follows a fixed plan, the Milestone
Executor gives the LLM full autonomy *within* each milestone's scope.
For each milestone it:

  1. Perceives the current environment
  2. Shows the LLM the milestone goal + success signal + results so far
  3. The LLM decides which tool to call next (or declares done)
  4. Executes the tool and feeds the result back to the LLM
  5. Repeats until the milestone is complete or the runtime safety cap is reached

This replaces the StepReasoner for complex, multi-milestone tasks —
the plan is defined by *outcomes* (milestones), not by specific
tool sequences.
"""

from __future__ import annotations

import json
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable
from functools import partial

from agent.planner import Milestone, MilestonePlan, MilestoneStatus
from platform_profile import platform_prompt_block
from providers import LLMProvider
from tools.selector import expand_milestone_hint_tools, resolve_milestone_allowed_tools

print = partial(print, flush=True)

# Raw browser tools hidden from the milestone executor's LLM prompt.
# They remain callable internally (the ACI compound tools use them),
# but the milestone LLM should use the higher-level ACI tools instead.
_RAW_BROWSER_TOOLS: frozenset[str] = frozenset({
    "browser_snapshot",
    "browser_find",
    "browser_click_match",
    "browser_click_ref",
    "browser_type_ref",
    "browser_select_ref",
    "browser_refresh_refs",
    "browser_scroll",
    "browser_wait_for",
    "browser_read_page",
    "browser_read_text",
    "browser_list_tabs",
    "browser_switch_tab",
})

_HARD_SAFETY_CAP = 50

# Import shared tool categories from the canonical source
from agent.constants import (
    UI_MUTATING_TOOLS as _UI_MUTATING_TOOLS,
    TRIVIAL_PROGRESS_TOOLS as _TRIVIAL_PROGRESS_TOOLS,
    LOW_SIGNAL_ACTION_TOOLS as _LOW_SIGNAL_ACTION_TOOLS,
)

_AWAIT_REPLY_SENTINEL = "AWAIT_REPLY:"

_FAILED_UI_RESULT_MARKERS: frozenset[str] = frozenset({
    "no ui element matching",
    "no text field matching",
    "no close match for",
    "visible elements:",
    "available elements:",
    "not expose it via accessibility",
    "try read_screen",
    "error in click_ui",
    "error in type_in_field",
    "failed to type text",
    "failed to paste text",
})


# ═══════════════════════════════════════════════════════════════
#  Data Types
# ═══════════════════════════════════════════════════════════════

@dataclass
class MilestoneAction:
    """Record of a single action taken within a milestone."""
    tool: str
    args: dict
    result: str = ""
    success: bool = False
    error: str = ""
    duration: float = 0.0


# ═══════════════════════════════════════════════════════════════
#  Prompts
# ═══════════════════════════════════════════════════════════════

MILESTONE_EXECUTOR_SYSTEM = """\
You are the execution engine for ClarityOS, a desktop AI assistant.
You are working on ONE milestone at a time. Your job: decide which tool to call
NEXT to achieve the milestone's goal, based on what you observe in the
environment and the results of previous actions.

CRITICAL RULES:
1. Focus ONLY on the current milestone's goal — don't try to complete future milestones.
2. Use the success_signal to know when you're done — once the signal is met, declare "done".
3. Adapt your approach based on tool results — if something fails, try a different way.
4. Be specific with tool arguments — use actual data from the environment/results.
5. For browser interactions: always perceive before acting. Read content before interacting.
5a. For web research, prefer `get_web_information(...)` over raw mechanism choices. It can search, choose a promising result, follow it in the browser, and then return page content/summary/structured items in one call when you provide a `query` with `target_type=page_content|page_summary|structured_data`. Use `target_type=search_results` only when you explicitly need the result list itself.
5b. After you get usable search results, FOLLOW THEM. Do not issue another similar search if you already have relevant result URLs. Open or read one of the authoritative sources next.
6. For writing tools (gdocs_create, gdocs_append): set "title" only. Body content is
   synthesized automatically from collected research. NEVER set "body" or "text".
7. NEVER fabricate data. If you need information, search/read for it first.
8. Output ONLY valid JSON — no markdown, no explanation outside the JSON.
9. Each action should make meaningful progress. Avoid repeating failed actions identically.
10. Use deliverables from previous milestones when available — they contain real data.
11. Prefer `web_scrape` over `run_python` for web extraction. Use `run_python` only as a last resort.
12. Active skill overlays are ADVISORY. Use them to guide strategy, but adapt to real observations and evidence.
13. In desktop chat apps (WhatsApp, Messages, Slack, Discord, Telegram), prefer `click_ui` and `type_in_field` to interact with the UI. Use `click_ui` to click the search bar or a contact name, and `type_in_field` to type a search query or message. Use `press_key` with key="Return" to send messages.
14. If a tool says it could not find the requested UI element or field, treat that as a failure and change strategy — do not count it as progress.
15. For "send it again" or repeat-message requests, reuse `last_typed_text` from the environment with `type_text`/`type_in_field`. Do not assume the clipboard contains the right message unless the clipboard content clearly matches.
16. Do NOT repeatedly call `open_app` for the same app — once is enough. If the app is open but you can't see the right content, use UI interaction tools (`click_ui`, `type_in_field`, `get_ui_tree`) to navigate within the app.
17. After `open_app`, ALWAYS call `read_screen` first before any other UI tool. This confirms the app is actually visible and gives you the UI layout for accurate interactions.
18. If `get_ui_tree` times out or returns nothing useful, fall back to `read_screen` + `click_ui` to interact visually instead. Do NOT retry `get_ui_tree` with the same arguments.
19. Do NOT use keyboard shortcuts (e.g., `command+f`) in desktop chat apps. Instead use `click_ui` to click buttons and UI elements directly — they are more reliable across different app versions.
20. If `type_in_field` cannot find a field, try `click_ui` to click the target area first, then use `type_text` to type into the now-focused field.
""" + platform_prompt_block() + """"""

MILESTONE_EXECUTOR_PROMPT = """\
## Overall Task
{task_summary}

## Current Milestone ({milestone_id}/{total_milestones})
Goal: {milestone_goal}
Success Signal: {success_signal}
Suggested Tools: {hint_tools}

## Deliverables from Previous Milestones
{deliverables}

## Active Skill Overlays
{skill_context}

## Recent Structured Observations
{observations}

## Search Leads
{search_leads}

## Actions Taken in This Milestone (count: {action_count})
{action_history}

## Stall Guidance
{stall_warning}

## Current Environment
{env_state}

## Last Action Result
{last_result}

## Available Tools
{tool_list}

## Decision Required
What tool should I call NEXT to achieve the milestone goal?
Or is the milestone already complete based on the results so far?

Return JSON:
{{
  "done": false,
  "tool": "tool_name",
  "args": {{}},
  "reasoning": "Why this action moves us toward the success signal",
  "description": "Human-readable description for the UI",
  "deliverable": null
}}

If the milestone is COMPLETE (success signal is met):
{{
  "done": true,
  "tool": "",
  "args": {{}},
  "reasoning": "How the success signal was met",
  "description": "Milestone complete",
  "deliverable": "The key result/data to pass to dependent milestones"
}}"""


# ═══════════════════════════════════════════════════════════════
#  Milestone Executor
# ═══════════════════════════════════════════════════════════════


# A 503 is worth one more try; a second one in the same request means the
# provider is down for us, and continuing only spends quota we need to deliver
# the failure message. The observed run went 503 -> retry -> ... -> 429.
_MAX_TRANSIENT_PROVIDER_RETRIES = 1


def _classify_provider_failure(error_text: Any) -> tuple[str, str]:
    """
    Return (kind, message) where kind is "fatal", "transient" or "".

    "fatal"     — quota exhausted, auth rejected, model gone: never retry.
    "transient" — 503/network: one bounded retry is allowed by the caller.
    """
    text = str(error_text or "")
    if not text:
        return "", ""

    # The shared classifier reads exc.code / exc.status, which a streamed error
    # *string* wrapped in Exception() does not carry — so every stream failure
    # came back UNKNOWN, and UNKNOWN is not retryable, so a 503 was reported
    # with the generic "The AI model failed to respond". Read the status out of
    # the text first; only fall through to the shared classifier otherwise.
    low = text.lower()
    # Status codes are matched as whole tokens, not substrings: a quota body
    # carrying "quotaValue: 1403" or a request id with 401 in it would
    # otherwise be reported to the user as a rejected API key.
    def _code(*codes: str) -> bool:
        # Neither digits nor letters may touch the code: "a401b" is a request
        # id, not an HTTP status.
        return any(re.search(rf"(?<![0-9a-z_]){c}(?![0-9a-z_])", low) for c in codes)

    if ("resource_exhausted" in low or "exceeded your current quota" in low
            or _code("429")):
        return "fatal", ("The configured Gemini model has reached its quota, so I can't "
                         "complete this request. Try again after the quota resets.")
    if ("api key not valid" in low or "permission_denied" in low
            or "unauthenticated" in low or "api_key_invalid" in low
            or _code("401", "403")):
        return "fatal", ("The Gemini API key was rejected. If you saved a key in "
                         "Settings → API keys it takes precedence over backend/.env — "
                         "clear it there to fall back to the .env key.")
    if any(w in low for w in ("timed out", "timeout", "connection reset",
                              "connection aborted", "temporarily unreachable",
                              "dns", "ssl", "network is unreachable")):
        return "transient", ("The connection to Gemini failed. I stopped rather than "
                             "keep retrying against it.")
    if "unavailable" in low or "overloaded" in low or _code("503"):
        return "transient", ("The Gemini API was temporarily unavailable (503). "
                             "I stopped rather than keep retrying against it.")

    fatal = _classify_fatal_provider_error(text)
    if fatal:
        return "fatal", fatal
    try:
        from providers.errors import classify_provider_error
        err = classify_provider_error(Exception(text))
        if err.retryable:
            return "transient", err.user_message()
    except Exception:
        pass
    return "", ""


def _classify_fatal_provider_error(error_text: Any) -> str:
    """
    Turn a streaming provider error into an actionable sentence, or "" when
    retrying could plausibly help.

    Reuses the shared classifier so the wording matches what the planner and
    the router already report for the same failure.
    """
    text = str(error_text or "")
    if not text:
        return ""
    try:
        from providers.errors import classify_provider_error
        err = classify_provider_error(Exception(text))
        if not err.retryable:
            return err.user_message()
    except Exception:
        pass
    low = text.lower()
    if "resource_exhausted" in low or "exceeded your current quota" in low or "429" in low:
        return ("The configured Gemini model has reached its daily free-tier quota, "
                "so I can't run this task right now. Try again after the quota resets.")
    if "api key not valid" in low or "permission_denied" in low or "unauthenticated" in low:
        return ("The Gemini API key was rejected. A key saved in Settings → API keys "
                "overrides backend/.env, so check it there first.")
    return ""


class MilestoneExecutor:
    """LLM micro-loop engine for milestone-based execution."""

    def __init__(
        self,
        provider: LLMProvider,
        tool_declarations: list[dict],
    ):
        self.provider = provider
        self._fatal_provider_error: str = ""
        self._transient_provider_failures: int = 0
        self._tool_list_cache: str = ""
        self._tool_declarations = tool_declarations
        self._build_tool_list()

    def _build_tool_list(self) -> None:
        """Build compact tool list for prompts (hides raw browser tools)."""
        self._tool_list_cache = self._format_tool_list()

    def _format_tool_list(self, allowed_tool_names: Optional[set[str]] = None) -> str:
        """Build compact tool list for prompts (hides raw browser tools)."""
        lines: list[str] = []
        for decl in self._tool_declarations:
            name = decl["name"]
            if allowed_tool_names is not None and name not in allowed_tool_names:
                continue
            if name in _RAW_BROWSER_TOOLS:
                continue  # Milestone LLM uses ACI tools instead
            desc = decl.get("description", "")
            params = decl.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            required = set(params.get("required", [])) if isinstance(params, dict) else set()
            param_parts: list[str] = []
            for pname, pschema in props.items():
                if pname == "reasoning":
                    continue  # Don't show injected reasoning param
                marker = "*" if pname in required else ""
                if isinstance(pschema, dict) and isinstance(pschema.get("enum"), list) and pschema["enum"]:
                    ptype = "{" + "|".join(str(v) for v in pschema["enum"][:8]) + "}"
                else:
                    ptype = pschema.get("type", "string") if isinstance(pschema, dict) else "string"
                param_parts.append(f"{pname}{marker}:{ptype}")
            param_str = ", ".join(param_parts) if param_parts else "(no args)"
            short_desc = desc[:100] + "…" if len(desc) > 100 else desc
            lines.append(f"  {name}({param_str}) — {short_desc}")
        return "\n".join(lines)

    def _expanded_hint_tools(self, milestone: Milestone) -> set[str]:
        return expand_milestone_hint_tools(milestone.hint_tools or [])

    def _resolve_allowed_tools(
        self,
        milestone: Milestone,
        request_scope_tool_names: Optional[set[str]],
    ) -> tuple[Optional[set[str]], Optional[set[str]]]:
        return resolve_milestone_allowed_tools(milestone.hint_tools or [], request_scope_tool_names)

    def _is_action_relevant_to_milestone(self, action: MilestoneAction, milestone: Milestone) -> bool:
        allowed = self._expanded_hint_tools(milestone)
        if allowed:
            return action.tool in allowed
        return True

    def _summarize_tool_result(self, result_text: str) -> str:
        raw = (result_text or "").strip()
        if not raw:
            return "  (none)"

        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            compact = " ".join(raw.split())
            return f"  {compact[:400]}"

        if not isinstance(data, dict):
            compact = " ".join(str(raw).split())
            return f"  {compact[:400]}"

        lines: list[str] = []
        title = str(data.get("title", "")).strip()
        url = str(data.get("url", "")).strip()
        page_type = str(data.get("page_type", "")).strip()
        if title:
            lines.append(f"  Title: {title[:160]}")
        if url:
            lines.append(f"  URL: {url[:220]}")
        if page_type:
            lines.append(f"  Page type: {page_type}")

        items = data.get("items")
        if isinstance(items, list) and items:
            for idx, item in enumerate(items[:5], 1):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "")).strip()
                context = str(item.get("context", "")).strip()
                href = str(item.get("href", "")).strip()
                rank = item.get("rank", idx)
                row = " | ".join(part for part in (label, context, href) if part)
                if row:
                    lines.append(f"  [{rank}] {row[:240]}")
            if lines:
                return "\n".join(lines[:8])

        content = str(data.get("content", "") or data.get("text", "") or data.get("summary", "")).strip()
        if content:
            compact_lines = [line.strip() for line in content.splitlines() if line.strip()]
            for line in compact_lines[:5]:
                lines.append(f"  {line[:220]}")
            return "\n".join(lines[:8])

        if data.get("message"):
            lines.append(f"  Message: {str(data.get('message'))[:220]}")
        if not lines:
            lines.append(f"  {raw[:300]}")
        return "\n".join(lines[:8])

    def _build_recent_observations(self, actions: list[MilestoneAction], last_result: str) -> str:
        recent_successes = [a for a in reversed(actions) if a.success][:3]
        sections: list[str] = []
        for action in reversed(recent_successes):
            summary = self._summarize_tool_result(action.result)
            sections.append(f"- {action.tool}\n{summary}")
        if not sections and last_result and last_result != "(no actions taken yet)":
            sections.append(f"- last_result\n{self._summarize_tool_result(last_result)}")
        return "\n".join(sections) if sections else "  (none yet)"

    def _build_search_leads(self, actions: list[MilestoneAction]) -> str:
        lines: list[str] = []
        seen_urls: set[str] = set()
        for action in reversed(actions):
            if not action.success:
                continue
            try:
                data = json.loads(action.result)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            target_type = str(data.get("target_type", "")).strip().lower()
            if target_type != "search_results":
                continue
            for item in data.get("items", [])[:5]:
                if not isinstance(item, dict):
                    continue
                href = str(item.get("href", "")).strip()
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)
                label = str(item.get("label", "")).strip()
                context = str(item.get("context", "")).strip()
                row = " | ".join(part for part in (label, context, href) if part)
                if row:
                    lines.append(f"  - {row[:240]}")
                if len(lines) >= 6:
                    return "\n".join(lines)
        return "\n".join(lines) if lines else "  (none yet)"

    async def _stream_llm_decision(
        self,
        prompt: str,
        ws_callback=None,
    ) -> tuple[Optional[str], float]:
        """Stream LLM response and parse JSON decision as early as possible.

        Uses generate_stream() to accumulate text chunks and attempts JSON
        parsing as soon as the accumulated text looks structurally complete
        (balanced braces).  Falls back to non-streaming generate() if the
        provider doesn't support streaming.

        Returns:
            (response_text, elapsed_seconds) or (None, elapsed) on failure.
        """
        t0 = _time.time()

        # ── Fast-path: provider supports streaming ──
        if hasattr(self.provider, "generate_stream"):
            accumulated = ""
            brace_depth = 0
            in_string = False
            escape_next = False
            json_complete = False

            try:
                async for chunk in self.provider.generate_stream(
                    messages=[{"role": "user", "parts": [{"text": prompt}]}],
                    system_prompt=MILESTONE_EXECUTOR_SYSTEM,
                    tools=[],
                    temperature=0.15,
                ):
                    if chunk.error:
                        print(f"[MilestoneExec] ⚠ Stream error: {chunk.error}")
                        # A quota/auth failure is not an "empty response" — it
                        # will fail identically on every retry. Record it so the
                        # decision loop stops at once and reports the real
                        # reason instead of stalling after three attempts.
                        kind, message = _classify_provider_failure(chunk.error)
                        if kind == "fatal":
                            self._fatal_provider_error = message
                        elif kind == "transient":
                            self._transient_provider_failures += 1
                            if self._transient_provider_failures > _MAX_TRANSIENT_PROVIDER_RETRIES:
                                print(f"[MilestoneExec] provider transient failure "
                                      f"#{self._transient_provider_failures} — stopping")
                                self._fatal_provider_error = message
                        break
                    if chunk.text:
                        accumulated += chunk.text

                        # Track brace depth for early JSON completion detection
                        for ch in chunk.text:
                            if escape_next:
                                escape_next = False
                                continue
                            if ch == "\\" and in_string:
                                escape_next = True
                                continue
                            if ch == '"' and not escape_next:
                                in_string = not in_string
                                continue
                            if in_string:
                                continue
                            if ch == "{":
                                brace_depth += 1
                            elif ch == "}":
                                brace_depth -= 1
                                if brace_depth == 0 and accumulated.strip():
                                    json_complete = True

                        if json_complete:
                            # JSON is structurally complete — stop streaming early
                            break

                elapsed = _time.time() - t0
                if accumulated.strip():
                    return accumulated, elapsed

            except Exception as e:
                print(f"[MilestoneExec] ⚠ Streaming failed, falling back: {e}")

        # ── Fallback: non-streaming generate() ──
        try:
            response = await self.provider.generate(
                messages=[{"role": "user", "parts": [{"text": prompt}]}],
                system_prompt=MILESTONE_EXECUTOR_SYSTEM,
                tools=[],
                temperature=0.15,
            )
            elapsed = _time.time() - t0
            return (response.text if response and response.text else None), elapsed
        except Exception as e:
            elapsed = _time.time() - t0
            print(f"[MilestoneExec] ⚠ LLM generate failed: {e}")
            return None, elapsed

    async def execute_milestone(
        self,
        milestone: Milestone,
        plan: MilestonePlan,
        env_perceiver: Callable[[], Awaitable[dict]],
        tool_executor: Callable[[str, dict], Awaitable[tuple[str, bool]]],
        deliverables: dict[int, str],
        skill_context: str = "",
        request_scope_tool_names: Optional[set[str]] = None,
        blocked_await_signatures: Optional[set[str]] = None,
        ws_callback=None,
    ) -> tuple[bool, str]:
        """Execute a single milestone via LLM micro-loop.

        Args:
            milestone: The milestone to execute
            plan: The full milestone plan (for context)
            env_perceiver: Async function returning environment state dict
            tool_executor: Async function (tool, args) -> (result_str, success_bool)
            deliverables: Results from completed milestones {id: deliverable_str}
            ws_callback: WebSocket callback for streaming updates

        Returns:
            Tuple of (success, result_summary)
        """
        actions: list[MilestoneAction] = []
        last_result = "(no actions taken yet)"

        print(f"\n[MilestoneExec] ═══ Milestone {milestone.id}: {milestone.goal} ═══")
        print(f"[MilestoneExec]   Success signal: {milestone.success_signal}")
        print(f"[MilestoneExec]   Hint tools: {milestone.hint_tools}")
        # ...existing code...
        priority_tool_names, fallback_tool_names = self._resolve_allowed_tools(milestone, request_scope_tool_names)
        blocked_awaits = set(blocked_await_signatures or set())
        tool_list_text = self._format_tool_list(priority_tool_names)

        # ── Glance: lightweight parallel screen awareness ──
        from agent.glance import get_glance
        glance = get_glance()
        _last_tool_name = ""       # tracks previous tool for app-change detection
        _last_tool_success = True  # tracks previous tool outcome

        action_num = 0
        decision_failures = 0
        consecutive_rejections = 0
        stall_warning = "  (none)"
        blocked_search_failures: dict[str, str] = {}
        # Cache successful get_web_information results to avoid redundant network calls
        _web_call_cache: dict[str, str] = {}
        # Track consecutive Flash timeouts per (url, target_type) for circuit-breaker hint
        _flash_timeout_registry: dict[str, int] = {}

        while action_num < _HARD_SAFETY_CAP:
            milestone.actions_taken = action_num

            # 0. Check for request cancellation
            from runtime_state import runtime_state_store as _rss
            if _rss.is_request_cancelled():
                print(f"[MilestoneExec] ⛔ Milestone {milestone.id} cancelled by user.")
                return False, "Cancelled by user."

            # 0b. The task budget is terminal for ordinary work. The registry
            # refuses each expired call individually, but the loop used to keep
            # asking the model what to try next — which is what kept calling
            # read_screen, get_ui_tree and gworkspace_analyze after the deadline
            # and spent the quota that the final message needed.
            from tools.registry import deadline_expired as _deadline_expired
            if _deadline_expired():
                print(f"[MilestoneExec] Milestone {milestone.id}: task deadline reached "
                      f"after {action_num} actions — stopping.")
                return False, (
                    "TASK_DEADLINE: ran out of time before this milestone could be "
                    "completed. No further tools were attempted."
                )

            # 1. Perceive environment
            env_state = await env_perceiver()

            # 1b. Glance: inject lightweight screen awareness
            #     peek() is ~0ms if cached, ~150ms if stale. Zero tokens.
            try:
                glance_result = await glance.peek()
                app_changed = glance.detect_app_change(glance_result)

                # If the last tool was open_app and the app actually changed,
                # or if the last UI tool failed, consider a deep look.
                if glance.should_deep_look(_last_tool_name, _last_tool_success, app_changed):
                    if ws_callback:
                        await ws_callback({
                            "type": "doing",
                            "text": "Looking at screen…",
                            "tool": "glance",
                            "variant": "looking",
                        })
                    glance_result = await glance.deep_look(
                        f"What app is currently in the foreground? "
                        f"I just ran '{_last_tool_name}'. Is the expected UI visible?"
                    )

                # Inject glance summary into environment when UI tools are likely
                _ui_tools = {"click_ui", "type_in_field", "click_element", "get_ui_tree",
                             "read_screen", "type_text", "press_key"}
                if (milestone.hint_tools and
                        any(ht in _ui_tools for ht in milestone.hint_tools)):
                    glance_ctx = glance.build_context("click_ui", glance_result)
                    if glance_ctx:
                        env_state["screen_glance"] = glance_ctx
            except Exception as e:
                print(f"[MilestoneExec] ⚠ Glance failed (non-fatal): {e}")

            env_lines = [f"  {k}: {v}" for k, v in env_state.items() if v]
            env_str = "\n".join(env_lines) if env_lines else "  (no environment data)"

            # 2. Build action history (last 3 only to reduce prompt size)
            history_lines: list[str] = []
            recent_actions = actions[-3:] if len(actions) > 3 else actions
            start_idx = len(actions) - len(recent_actions) + 1
            if len(actions) > 3:
                history_lines.append(f"  ... ({len(actions) - 3} earlier actions omitted)")
            for i, a in enumerate(recent_actions, start_idx):
                icon = "✓" if a.success else "✗"
                result_preview = (a.result[:150] if a.result else a.error[:100]).replace("\n", " ")
                history_lines.append(f"  {icon} Action {i}: {a.tool} → {result_preview}")
            action_history = "\n".join(history_lines) if history_lines else "  (none yet)"

            # 3. Build deliverables summary (truncate each to 200 chars)
            deliv_lines: list[str] = []
            for mid, dval in deliverables.items():
                m = next((m for m in plan.milestones if m.id == mid), None)
                label = m.goal if m else f"Milestone {mid}"
                deliv_lines.append(f"  [{mid}] {label}: {str(dval)[:200]}")
            deliv_str = "\n".join(deliv_lines) if deliv_lines else "  (none — this is the first milestone)"

            # 4. Build prompt
            prompt = MILESTONE_EXECUTOR_PROMPT.format(
                task_summary=plan.task_summary,
                milestone_id=milestone.id,
                total_milestones=len(plan.milestones),
                milestone_goal=milestone.goal,
                success_signal=milestone.success_signal,
                hint_tools=", ".join(milestone.hint_tools) if milestone.hint_tools else "(any)",
                deliverables=deliv_str,
                skill_context=skill_context or getattr(plan, "skill_context", "") or "(none)",
                observations=self._build_recent_observations(actions, last_result),
                search_leads=self._build_search_leads(actions),
                action_count=action_num,
                action_history=action_history,
                stall_warning=stall_warning,
                env_state=env_str,
                last_result=last_result[:800] if last_result else "(none)",
                tool_list=tool_list_text,
            )

            # 5. Ask LLM what to do (streaming with early JSON parse)
            t_llm = _time.time()
            try:
                response_text, llm_elapsed = await self._stream_llm_decision(
                    prompt, ws_callback=ws_callback,
                )

                if not response_text:
                    fatal = getattr(self, "_fatal_provider_error", "")
                    if fatal:
                        print(f"[MilestoneExec] ✗ Provider unavailable — stopping: {fatal}")
                        break
                    print(f"[MilestoneExec] ⚠ Empty LLM response at action {action_num + 1}")
                    decision_failures += 1
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: too many empty LLM responses")
                        break
                    continue

                decision = self._parse_decision(response_text)
                print(
                    f"[MilestoneExec]   Action {action_num + 1}: "
                    f"{'DONE' if decision.get('done') else decision.get('tool', '?')} "
                    f"({llm_elapsed:.2f}s) — {decision.get('reasoning', '')[:100]}"
                )

            except Exception as e:
                print(f"[MilestoneExec] ⚠ LLM decision failed: {e}")
                decision_failures += 1
                if decision_failures >= 3:
                    print(f"[MilestoneExec] ⚠ Stalled: too many LLM errors")
                    break
                continue

            # 6. Check if done
            if decision.get("done", False):
                deliverable = decision.get("deliverable", "")
                reasoning = decision.get("reasoning", "")
                evidence_ok, evidence_reason = self._completion_has_evidence(
                    milestone=milestone,
                    actions=actions,
                    deliverable=str(deliverable or reasoning or ""),
                )
                if not evidence_ok:
                    decision_failures += 1
                    last_result = f"Completion rejected: {evidence_reason}"
                    stall_warning = f"  Completion rejected: {evidence_reason}"
                    print(f"[MilestoneExec] ⚠ Completion rejected: {evidence_reason}")
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: repeated unsupported completion claims")
                        break
                    continue
                milestone.result_summary = str(deliverable or reasoning)[:500]
                milestone.actions_taken = action_num
                print(f"[MilestoneExec] ✅ Milestone {milestone.id} COMPLETE: {reasoning[:150]}")
                return True, str(deliverable or reasoning)

            # 7. Execute the tool
            tool = decision.get("tool", "")
            args = decision.get("args", {})
            description = decision.get("description", "")

            if not tool:
                print(f"[MilestoneExec] ⚠ No tool specified, skipping action")
                decision_failures += 1
                if decision_failures >= 3:
                    print(f"[MilestoneExec] ⚠ Stalled: too many empty tool decisions")
                    break
                continue

            if priority_tool_names is not None and tool not in priority_tool_names:
                # Two-tier: check if tool is in the fallback set
                if fallback_tool_names is not None and tool in fallback_tool_names:
                    # Soft warning — accept but nudge toward priority tools
                    last_result = (
                        f"Tool '{tool}' executed (outside suggested set). "
                        f"Prefer: {', '.join(sorted(list(priority_tool_names)[:8]))}"
                    )
                    consecutive_rejections = 0
                    print(f"[MilestoneExec] ⚡ {tool} accepted via fallback tier")
                    # Fall through to execution below
                else:
                    # Hard rejection — truly unknown tool
                    consecutive_rejections += 1
                    decision_failures += 1
                    last_result = (
                        f"Tool '{tool}' is not available. "
                        f"Use one of: {', '.join(sorted(list(priority_tool_names)[:10]))}"
                    )
                    stall_warning = f"  {last_result}"
                    print(f"[MilestoneExec] ⚠ {last_result}")
                    # Auto-expand on stall: after 2 consecutive rejections,
                    # promote all fallback tools to priority.
                    if consecutive_rejections >= 2 and fallback_tool_names:
                        priority_tool_names = set(fallback_tool_names)
                        tool_list_text = self._format_tool_list(priority_tool_names)
                        consecutive_rejections = 0
                        decision_failures = max(0, decision_failures - 1)
                        print(f"[MilestoneExec] 🔓 Auto-expanded tool scope to {len(priority_tool_names)} tools")
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: repeated out-of-scope tool selections")
                        break
                    continue

            if tool == "await_reply":
                await_signature = self._stable_args(args)
                if await_signature in blocked_awaits:
                    decision_failures += 1
                    last_result = (
                        "Repeated await_reply request blocked for this milestone. "
                        "Use the user's latest reply or choose a different tool."
                    )
                    stall_warning = f"  {last_result}"
                    print(f"[MilestoneExec] ⚠ {last_result}")
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: repeated await_reply selections")
                        break
                    continue
            search_retry_key = self._search_retry_key(tool, args)
            if search_retry_key and search_retry_key in blocked_search_failures:
                decision_failures += 1
                last_result = blocked_search_failures[search_retry_key]
                stall_warning = f"  {last_result}"
                print(f"[MilestoneExec] ⚠ {last_result}")
                if decision_failures >= 3:
                    print(f"[MilestoneExec] ⚠ Stalled: repeated failed search selections")
                    break
                continue

            # Cache hit: serve a previously successful get_web_information result without
            # a network round-trip.  Key on (target_type, url, query) — same call same result.
            if tool == "get_web_information":
                _t = str((args or {}).get("target_type", "")).strip().lower()
                _u = str((args or {}).get("url", "") or (args or {}).get("source_url", "") or "").strip().rstrip("/")
                _q = " ".join(str((args or {}).get("query", "")).strip().lower().split())
                _wck = f"{_t}|{_u}|{_q}"
                if _wck in _web_call_cache:
                    _cached = _web_call_cache[_wck]
                    print(f"[MilestoneExec] ⚡ Cache hit {_wck[:60]} — reusing {len(_cached)}ch result")
                    action = MilestoneAction(
                        tool=tool, args=args, result=_cached, success=True, error="", duration=0.0,
                    )
                    actions.append(action)
                    last_result = _cached[:800]
                    action_num += 1
                    _cr, _nsw = self._detect_stall(actions)
                    if not _cr:
                        break
                    stall_warning = f"  {_nsw}" if _nsw else "  (none)"
                    continue
            decision_failures = 0

            # Stream progress to UI
            if ws_callback:
                _BROWSER_TOOLS = {
                    "get_web_information", "web_search", "open_url",
                    "browser_click_ref", "browser_type_ref", "browser_select_ref",
                    "browser_scroll", "browser_read_page", "browser_read_text",
                    "read_page_content", "get_page_summary", "extract_structured_data",
                    "browser_click_match", "browser_find", "browser_describe_ref",
                }
                _UI_INTERACTION_TOOLS = {
                    "click_ui", "type_in_field", "click_element",
                    "type_text", "press_key", "get_ui_tree",
                }
                if tool in _BROWSER_TOOLS:
                    tool_variant = "browsing"
                elif tool in _UI_INTERACTION_TOOLS:
                    tool_variant = "looking"
                else:
                    tool_variant = ""
                # `description` is the model's account of what it is about to
                # do, emitted before the tool runs. It claimed "Summarizing the
                # code…" with nothing retrieved, and "Opening and focusing VS
                # Code" on steps that only read. Reconcile it with the tool
                # actually being called and with what has really been retrieved.
                from agent.retrieval_guard import (
                    progress_text as _progress_text,
                    last_failure_reason as _last_failure,
                )
                _raw_text = description or f"Milestone {milestone.id}: {tool}"
                _text = _progress_text(_raw_text, tool=tool)
                if _text is None:
                    await ws_callback({
                        "type": "doing",
                        "text": f"Couldn't retrieve the content for this step. "
                                f"{_last_failure()}".strip(),
                        "tool": tool,
                        "variant": "failed",
                    })
                else:
                    await ws_callback({
                        "type": "doing",
                        "text": _text,
                        "tool": tool,
                        "variant": tool_variant,
                    })

            # Glance: quick peek before UI tools so the LLM has fresh element data
            if glance.should_peek_before(tool):
                try:
                    pre_glance = await glance.peek()
                    if pre_glance.element_count > 0:
                        print(f"[Glance] 👁 Pre-{tool}: {pre_glance.element_count} elements, "
                              f"{len(pre_glance.text_fields)} fields, "
                              f"{len(pre_glance.buttons)} buttons")
                except Exception:
                    pass

            t_exec = _time.time()
            result_str, success = await tool_executor(tool, args)
            exec_elapsed = _time.time() - t_exec

            # Track for next-iteration glance decisions
            _last_tool_name = tool
            _last_tool_success = success

            # Glance: post-execution verification for app-switching tools
            if glance.should_peek_after(tool) and success:
                try:
                    # Invalidate cache so we get fresh data from new app
                    glance._cache = None
                    post_glance = await glance.refresh()
                    expected_app = (args or {}).get("app_name", "")
                    if (expected_app and
                            expected_app.lower() not in post_glance.active_app.lower() and
                            post_glance.active_app.lower() not in expected_app.lower()):
                        # App didn't actually come to front — warn the LLM
                        result_str += (
                            f" ⚠ Glance: active app is '{post_glance.active_app}', "
                            f"not '{expected_app}'. The target app may not have come to the foreground."
                        )
                        print(f"[Glance] ⚠ Post-open_app: expected '{expected_app}' "
                              f"but active is '{post_glance.active_app}'")
                    else:
                        print(f"[Glance] ✓ Post-open_app: '{post_glance.active_app}' confirmed")
                except Exception as e:
                    print(f"[Glance] ⚠ Post-execution peek failed: {e}")

            action = MilestoneAction(
                tool=tool,
                args=args,
                result=result_str if success else "",
                success=success,
                error=result_str if not success else "",
                duration=exec_elapsed,
            )
            actions.append(action)
            last_result = result_str[:800] if result_str else "(empty result)"

            # Update web call cache and flash-timeout circuit breaker
            if tool == "get_web_information":
                _t = str((args or {}).get("target_type", "")).strip().lower()
                _u = str((args or {}).get("url", "") or (args or {}).get("source_url", "") or "").strip().rstrip("/")
                _q = " ".join(str((args or {}).get("query", "")).strip().lower().split())
                _wck = f"{_t}|{_u}|{_q}"
                if success:
                    _web_call_cache[_wck] = result_str
                    _flash_timeout_registry.pop(_wck, None)  # reset on success
                elif "timed out" in (result_str or "").lower():
                    _flash_timeout_registry[_wck] = _flash_timeout_registry.get(_wck, 0) + 1
                    if _flash_timeout_registry[_wck] >= 2:
                        result_str = (
                            f"⚡ Flash timed out {_flash_timeout_registry[_wck]}× for this URL+type. "
                            f"Switch to target_type='page_content' for this URL instead of '{_t}'."
                        )
                        last_result = result_str

            print(
                f"[MilestoneExec]   {'✓' if success else '✗'} {tool} ({exec_elapsed:.2f}s) "
                f"→ {last_result[:120]}"
            )

            if not success:
                search_retry_key = self._search_retry_key(tool, args)
                failure_summary = self._search_failure_summary(tool, args, result_str)
                if search_retry_key and failure_summary:
                    blocked_search_failures[search_retry_key] = failure_summary

                # ── Auto-screen-recovery: when a UI tool fails, automatically
                #    capture the screen so the LLM can SEE what went wrong ──
                if tool in _UI_MUTATING_TOOLS:
                    try:
                        screen_result, screen_ok = await tool_executor(
                            "read_screen",
                            {"question": f"What is on screen? The previous action '{tool}' failed."},
                        )
                        if screen_ok and screen_result:
                            auto_action = MilestoneAction(
                                tool="read_screen",
                                args={"question": "(auto-recovery after failed UI action)"},
                                result=screen_result,
                                success=True,
                                error="",
                                duration=0.0,
                            )
                            actions.append(auto_action)
                            last_result = (
                                f"[AUTO-RECOVERY] Previous action '{tool}' failed. "
                                f"Here is what the screen currently shows:\n"
                                f"{screen_result[:600]}"
                            )
                            print(f"[MilestoneExec] 👁 Auto-recovery: captured screen after failed {tool}")
                    except Exception as e:
                        print(f"[MilestoneExec] ⚠ Auto-recovery read_screen failed: {e}")

            if tool == "await_reply" and success:
                await_signature = self._stable_args(args)
                payload = {
                    "milestone_id": milestone.id,
                    "message": result_str,
                    "signature": await_signature,
                }
                milestone.actions_taken = action_num + 1
                milestone.result_summary = result_str[:500]
                return False, _AWAIT_REPLY_SENTINEL + json.dumps(payload, ensure_ascii=False)

            action_num += 1
            continue_running, next_stall_warning = self._detect_stall(actions)
            if not continue_running:
                print(f"[MilestoneExec] ⚠ Stalled: {next_stall_warning}")
                break
            stall_warning = f"  {next_stall_warning}" if next_stall_warning else "  (none)"

        milestone.actions_taken = action_num
        if action_num >= _HARD_SAFETY_CAP:
            print(f"[MilestoneExec] ⚠ Milestone {milestone.id} hit hard safety cap ({_HARD_SAFETY_CAP})")
        else:
            print(f"[MilestoneExec] ⚠ Milestone {milestone.id} stalled after {action_num} actions")
        milestone.result_summary = f"Stalled after {action_num} actions. Last result: {last_result[:200]}"

        substantive_actions = [
            a for a in actions
            if self._is_substantive_result(a) and self._is_action_relevant_to_milestone(a, milestone)
        ]
        if substantive_actions:
            return True, milestone.result_summary

        return False, milestone.result_summary

    def _stable_args(self, args: dict) -> str:
        """Normalize args for repeat-detection."""
        try:
            return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(args or {})

    def _search_retry_key(self, tool: str, args: dict) -> str:
        if tool != "get_web_information":
            return ""
        target_type = str((args or {}).get("target_type", "")).strip().lower()
        url = str((args or {}).get("url", "") or (args or {}).get("source_url", "") or "").strip().rstrip("/")
        query = " ".join(str((args or {}).get("query", "")).strip().lower().split())
        # Key covers all target_types so repeated failures on page_content / structured_data
        # are blocked the same way as repeated failed searches.
        return f"{tool}|{target_type}|{url}|{query}"

    def _search_failure_summary(self, tool: str, args: dict, result_text: str) -> str:
        retry_key = self._search_retry_key(tool, args)
        if not retry_key:
            return ""
        error_code = "failed"
        route = ""
        try:
            data = json.loads(result_text)
        except (TypeError, ValueError):
            data = {}
        if isinstance(data, dict):
            error_code = str(data.get("error_code", "") or "failed").strip().lower()
            route = str(data.get("route", "")).strip().lower()
        query = str((args or {}).get("query", "")).strip()
        route_label = f" via {route}" if route else ""
        return (
            f"Repeated failed search blocked for '{query}' "
            f"({error_code or 'failed'}{route_label}). Use a different query or tool."
        )

    def _is_known_empty_json(self, result_text: str) -> bool:
        """Detect structured tool outputs that carry no useful payload."""
        try:
            data = json.loads(result_text)
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict):
            return False

        if int(data.get("item_count", 1) or 0) == 0:
            return True
        if int(data.get("content_length", 1) or 0) == 0:
            return True
        if int(data.get("total_elements", 1) or 0) == 0:
            return True
        if "items" in data and isinstance(data.get("items"), list) and len(data.get("items", [])) == 0:
            return True
        return False

    # Tools whose output is legitimately short (confirmations, status messages).
    # These should never count as "zero-yield" even when the result is < 60 chars.
    _SHORT_OUTPUT_TOOLS: frozenset[str] = frozenset({
        "open_app", "quit_app", "close_window", "press_key",
        "run_shortcut", "type_text", "type_in_field", "click_ui",
        "browser_scroll", "browser_switch_tab", "open_url",
        "send_response", "await_reply", "send_imessage",
    })

    def _is_zero_yield_action(self, action: MilestoneAction) -> bool:
        """Zero-yield = tool call succeeded but produced no substantive output."""
        if not action.success:
            return False
        # Short-output tools legitimately return brief confirmations
        if action.tool in self._SHORT_OUTPUT_TOOLS:
            return False
        result_text = (action.result or "").strip()
        if len(result_text) < 30:
            return True
        return self._is_known_empty_json(result_text)

    def _detect_stall(self, actions: list[MilestoneAction]) -> tuple[bool, str]:
        """Detect milestone stalls and produce warning text for next LLM turn."""
        warning = ""

        # Count consecutive identical actions (same tool + same args)
        identical_streak = 0
        if len(actions) >= 2:
            last = actions[-1]
            last_sig = f"{last.tool}|{self._stable_args(last.args)}"
            for action in reversed(actions):
                sig = f"{action.tool}|{self._stable_args(action.args)}"
                if sig == last_sig:
                    identical_streak += 1
                else:
                    break
            if identical_streak >= 3:
                return False, f"three consecutive identical {last.tool} calls with same args — the approach is not working"
            if identical_streak >= 2:
                warning = (
                    f"Repeated identical action detected: {last.tool} with same args "
                    f"({identical_streak}×). You MUST try a completely different tool or approach. "
                    "For messaging apps: use click_ui or type_in_field to interact with the UI."
                )

        zero_yield_streak = 0
        for action in reversed(actions):
            if self._is_zero_yield_action(action):
                zero_yield_streak += 1
            else:
                break
        if zero_yield_streak >= 4:
            return False, "four consecutive zero-yield actions"

        recent_search_queries: list[str] = []
        recent_search_with_items = 0
        for action in reversed(actions):
            if not action.success:
                break
            try:
                data = json.loads(action.result)
            except (TypeError, ValueError):
                break
            if not isinstance(data, dict):
                break
            target_type = str(data.get("target_type", "")).strip().lower()
            if action.tool != "get_web_information" or target_type != "search_results":
                break
            if int(data.get("item_count", 0) or 0) <= 0:
                break
            recent_search_with_items += 1
            recent_search_queries.append(str(action.args.get("query", "")).strip().lower())

        if recent_search_with_items >= 3:
            return False, "repeated search-result gathering without following a source"
        if recent_search_with_items >= 2:
            warning = (
                "You already have search results with source URLs. Stop searching and open/read one of those sources next."
            )

        repeated_url_reads = 0
        last_url = ""
        last_target_type = ""
        for action in reversed(actions):
            if not action.success or action.tool != "get_web_information":
                break
            try:
                data = json.loads(action.result)
            except (TypeError, ValueError):
                break
            if not isinstance(data, dict):
                break
            target_type = str(data.get("target_type", "")).strip().lower()
            current_url = str(data.get("url", "")).strip().rstrip("/")
            if target_type not in {"page_content", "page_summary", "structured_data"} or not current_url:
                break
            if not last_url:
                last_url = current_url
                last_target_type = target_type
                repeated_url_reads = 1
                continue
            if current_url == last_url and target_type == last_target_type:
                repeated_url_reads += 1
            else:
                break

        if repeated_url_reads >= 2:
            return False, "repeated reading of the same source without new evidence"

        return True, warning

    def _is_substantive_result(self, action: MilestoneAction) -> bool:
        """Substantive result threshold for partial-success fallback."""
        if not action.success:
            return False
        if action.tool in _TRIVIAL_PROGRESS_TOOLS:
            return False
        # Short-output tools (open_app, click_ui, etc.) are substantive by nature
        if action.tool in self._SHORT_OUTPUT_TOOLS:
            return True
        result_text = (action.result or "").strip()
        result_lower = result_text.lower()
        if any(marker in result_lower for marker in _FAILED_UI_RESULT_MARKERS):
            return False
        if len(result_text) <= 100:
            return False
        if self._is_known_empty_json(result_text):
            return False
        return True

    def _has_observable_action_progress(self, action: MilestoneAction) -> bool:
        if not action.success:
            return False
        if action.tool in _LOW_SIGNAL_ACTION_TOOLS:
            return False
        result_text = (action.result or "").strip()
        result_lower = result_text.lower()
        if any(marker in result_lower for marker in _FAILED_UI_RESULT_MARKERS):
            return False
        if self._is_known_empty_json(result_text):
            return False
        return bool(result_text)

    def _completion_has_evidence(
        self,
        milestone: Milestone,
        actions: list[MilestoneAction],
        deliverable: str,
    ) -> tuple[bool, str]:
        deliverable_text = (deliverable or "").strip()
        goal_text = f"{milestone.goal} {milestone.success_signal}".lower()
        evidence_sensitive_markers = (
            "research", "source", "read", "extract", "listing", "result",
            "link", "compare", "price", "market", "document", "report",
        )
        evidence_sensitive = any(marker in goal_text for marker in evidence_sensitive_markers)

        if not actions:
            # Allow the LLM to declare DONE on turn 1 if it provides a
            # meaningful deliverable — the task may already be satisfied.
            if deliverable_text:
                return True, ""
            return False, "no actions executed yet"

        substantive_actions = [
            action for action in actions
            if self._is_substantive_result(action) and self._is_action_relevant_to_milestone(action, milestone)
        ]
        if substantive_actions:
            return True, ""

        if len(deliverable_text) >= 120:
            if any(
                self._is_action_relevant_to_milestone(action, milestone)
                and self._has_observable_action_progress(action)
                for action in actions
            ):
                return True, ""
        if "http" in deliverable_text and len(deliverable_text) >= 40:
            if any(
                self._is_action_relevant_to_milestone(action, milestone)
                and self._has_observable_action_progress(action)
                for action in actions
            ):
                return True, ""

        successful_nontrivial = [
            action for action in actions
            if (
                action.success
                and action.tool not in _TRIVIAL_PROGRESS_TOOLS
                and not self._is_zero_yield_action(action)
                and self._is_action_relevant_to_milestone(action, milestone)
            )
        ]
        if successful_nontrivial:
            return True, ""

        if deliverable_text and len(deliverable_text) >= 20:
            if any(
                self._is_action_relevant_to_milestone(action, milestone)
                and self._has_observable_action_progress(action)
                for action in actions
            ):
                return True, ""

        if evidence_sensitive:
            return False, "success signal not backed by extracted evidence yet"

        return False, "insufficient evidence for milestone completion"

    def _parse_decision(self, text: str) -> dict:
        """Parse the LLM's JSON decision."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            first_nl = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_nl + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end])
                except json.JSONDecodeError:
                    pass
            print(f"[MilestoneExec] ⚠ Could not parse decision JSON")
            return {"done": False, "tool": "", "args": {}, "reasoning": "Parse error"}


# ═══════════════════════════════════════════════════════════════
#  Factory / Singleton
# ═══════════════════════════════════════════════════════════════

_instance: Optional[MilestoneExecutor] = None


def get_milestone_executor(
    provider: LLMProvider,
    tool_declarations: list[dict],
) -> MilestoneExecutor:
    """Get or create the milestone executor singleton."""
    global _instance
    if _instance is None or _instance.provider is not provider:
        _instance = MilestoneExecutor(provider, tool_declarations)
    else:
        _instance._tool_declarations = tool_declarations
        _instance._build_tool_list()
    # Per-run reset: a quota failure from an earlier request must not be
    # reported as the reason a later, unrelated milestone failed.
    _instance._fatal_provider_error = ""
    # Same reasoning for the transient (503) budget: it is per request, and the
    # executor is a singleton, so a 503 seen an hour ago must not make the next
    # request give up on its first one.
    _instance._transient_provider_failures = 0
    return _instance
