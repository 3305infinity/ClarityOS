"""
ClarityOS — Task Planner V2
===========================
Milestone-first task planner for the unified execution loop.
"""

import json
import time
from typing import Optional, List
from functools import partial

print = partial(print, flush=True)

from agent.world_state import WorldState, UserIntent, IntentAction, TargetType, IntentParser, TaskGraph
from agent.planner import Milestone, MilestonePlan, MilestoneStatus
from agent.example_bank import ExampleBank
from agent.template_registry import TemplateRegistry
from platform_profile import platform_prompt_block
from providers.errors import ProviderError, classify_provider_error
from tools.selector import get_tool_selector


def _norm_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


# ═══════════════════════════════════════════════════════════════
#  Milestone Planning Prompt (V3)
# ═══════════════════════════════════════════════════════════════

MILESTONE_PLANNING_SYSTEM = """You are the milestone planner for ClarityOS, a desktop AI assistant.
Your job: convert user requests into MILESTONE plans — high-level goal checkpoints.
Output ONLY valid JSON — no explanations, no markdown.

## Key Principle
You define WHAT must be accomplished, not HOW. Each milestone is a goal
with a success signal. The runtime executor decides which tools to call.

## Milestone Design Rules
1. Each milestone is a distinct GOAL, not a tool call.
2. The success_signal must be OBSERVABLE — something the executor can verify.
3. Use 1 milestone for simple direct tasks and 2-6 milestones for compound tasks. Don't micro-manage.
4. hint_tools are SUGGESTIONS, not prescriptions. The executor may use different tools.
5. Do not include max_actions. Runtime safety limits are enforced by the executor.
6. deliverable_key names the output stored in working memory for downstream milestones.

## Voice Transcription Awareness
Requests arrive as voice-transcribed text. Correct obvious errors.
""" + platform_prompt_block() + """"""

MILESTONE_PLANNING_PROMPT = """Given this user request and context, create a milestone plan.

## Available Tools
{tool_categories}

## Current Desktop State
{world_state}

## Conversation History
{conversation_context}

## Active Skill Overlays
{skill_context}

## User Request
"{user_request}"

## Output Format
Return a JSON object with this exact structure:
{{
  "task_summary": "Brief description of the full task",
  "needs_clarification": false,
  "clarification_prompt": "",
  "milestones": [
    {{
      "id": 1,
      "goal": "What must be accomplished (natural language)",
      "success_signal": "Observable evidence that the goal is met",
      "hint_tools": ["tool_category_1", "tool_category_2"],
      "depends_on": [],
      "deliverable_key": "key_name"
    }}
  ],
  "final_response": "Short confirmation message to show user"
}}

## Milestone Design Examples

Request: "Research UK rental market and create a document"
{{
  "task_summary": "Research UK rental market and create a report",
  "needs_clarification": false,
  "milestones": [
    {{
      "id": 1,
      "goal": "Search for UK rental market data and identify 3+ authoritative sources",
      "success_signal": "At least 3 source URLs visited and content extracted",
      "hint_tools": ["get_web_information", "open_url"],
      "depends_on": [],
      "deliverable_key": "research_data"
    }},
    {{
      "id": 2,
      "goal": "Create a comprehensive Google Doc with findings",
      "success_signal": "Google Doc URL returned with content written",
      "hint_tools": ["gdocs_create", "gdocs_append"],
      "depends_on": [1],
      "deliverable_key": "document_url"
    }}
  ],
  "final_response": "Research complete! Here's your document."
}}

Request: "Compare prices of MacBook Pro vs Dell XPS on Amazon"
{{
  "task_summary": "Compare MacBook Pro and Dell XPS prices on Amazon",
  "needs_clarification": false,
  "milestones": [
    {{
      "id": 1,
      "goal": "Find MacBook Pro price on Amazon",
      "success_signal": "MacBook Pro price and model details extracted",
      "hint_tools": ["get_web_information", "open_url"],
      "depends_on": [],
      "deliverable_key": "macbook_price"
    }},
    {{
      "id": 2,
      "goal": "Find Dell XPS price on Amazon",
      "success_signal": "Dell XPS price and model details extracted",
      "hint_tools": ["get_web_information", "open_url"],
      "depends_on": [],
      "deliverable_key": "dell_price"
    }},
    {{
      "id": 3,
      "goal": "Present comparison to user",
      "success_signal": "Comparison table or summary delivered",
      "hint_tools": ["send_response"],
      "depends_on": [1, 2],
      "deliverable_key": "comparison"
    }}
  ],
  "final_response": ""
}}

Request: "Open Spotify"
{{
  "task_summary": "Open Spotify",
  "needs_clarification": false,
  "clarification_prompt": "",
  "milestones": [
    {{
      "id": 1,
      "goal": "Open the Spotify application",
      "success_signal": "Spotify is open and focused",
      "hint_tools": ["open_app"],
      "depends_on": [],
      "deliverable_key": "opened_app"
    }}
  ],
  "final_response": "Spotify is open."
}}

Request: "Message Chris on WhatsApp saying I'll be late"
{{
  "task_summary": "Send a WhatsApp message to Chris",
  "needs_clarification": false,
  "clarification_prompt": "",
  "milestones": [
    {{
      "id": 1,
      "goal": "Open WhatsApp, search for and open the chat with Chris",
      "success_signal": "WhatsApp is open and Chris's chat conversation is visible",
      "hint_tools": ["open_app", "click_ui", "type_in_field"],
      "depends_on": [],
      "deliverable_key": "chat_opened"
    }},
    {{
      "id": 2,
      "goal": "Type the message and send it to Chris",
      "success_signal": "The message has been typed into the chat and sent",
      "hint_tools": ["click_ui", "type_in_field", "type_text", "press_key"],
      "depends_on": [1],
      "deliverable_key": "message_sent"
    }}
  ],
  "final_response": "Message sent to Chris on WhatsApp."
}}

Now create a milestone plan for the user's request. Output ONLY the JSON:"""


REPLAN_PROMPT = """A milestone plan is being executed but milestone {failed_id} FAILED.
Revise the REMAINING milestones to recover and still achieve the original goal.

## Original User Request
"{user_request}"

## Original Plan Summary
{task_summary}

## Completed Milestones (DO NOT repeat)
{completed_summary}

## Failed Milestone
Goal: {failed_goal}
Error: {failure_reason}

## Remaining Milestones (THESE need revision)
{remaining_summary}

## Available Deliverables
{deliverables_summary}

## Instructions
- Revise the remaining milestones to work around the failure.
- You may add, remove, or merge milestones.
- Preserve deliverable keys that downstream milestones depend on.
- If the failure is unrecoverable, return an empty milestones array.
- Milestone IDs should continue from {next_id}.
- Output ONLY the JSON — no explanations.

## Output Format
{{
  "milestones": [
    {{
      "id": {next_id},
      "goal": "Revised goal",
      "success_signal": "Observable evidence",
      "hint_tools": ["tool_1"],
      "depends_on": [],
      "deliverable_key": "key_name"
    }}
  ],
  "recovery_strategy": "Brief explanation of how the revised plan recovers"
}}"""


# ═══════════════════════════════════════════════════════════════
#  Task Planner Class
# ═══════════════════════════════════════════════════════════════

class TaskPlanner:
    """
    Generates milestone plans from user requests.

    The active runtime is milestone-only.
    """
    
    def __init__(self, provider=None, tool_registry=None):
        """
        Initialize the task planner.
        
        Args:
            provider: LLM provider for complex planning (optional)
            tool_registry: Tool registry for getting tool declarations
        """
        self.provider = provider
        self.tool_registry = tool_registry
        self.intent_parser = IntentParser()
        self.example_bank = ExampleBank()
        self.template_registry = TemplateRegistry()
        
        # Cache for tool descriptions
        self._tool_descriptions_cache: Optional[str] = None

        # Classified reason the last planning call failed (quota, auth, network…).
        self.last_provider_error: Optional[ProviderError] = None

    def record_success(self, request: str, plan, intent: UserIntent):
        """Record a successful plan execution in the example bank for future learning."""
        if plan.needs_clarification:
            return
        # Only record LLM-generated plans (templates don't need learning)
        if getattr(plan, "source", "") in ("template", "template_pack"):
            return
        try:
            if isinstance(plan, MilestonePlan):
                if not plan.milestones:
                    return
                plan_dict = plan.to_dict()
                tools = sorted({
                    tool
                    for milestone in plan.milestones
                    for tool in milestone.hint_tools
                    if tool
                })
            else:
                if not plan.steps:
                    return
                plan_dict = {
                    "task_summary": plan.task_summary,
                    "steps": [s.to_dict() for s in plan.steps],
                    "final_response": plan.final_response,
                }
                tools = [s.tool for s in plan.steps]
            self.example_bank.record(
                request=request,
                intent_action=intent.action.value,
                intent_target=intent.target_type.value,
                plan_json=plan_dict,
                tools_used=tools,
                success=True,
            )
        except Exception:
            pass  # Non-critical

    def _looks_like_simple_media_open(self, user_request: str, intent: UserIntent) -> bool:
        text = f" {_norm_text(user_request)} "
        if intent.action not in {IntentAction.OPEN, IntentAction.PLAY}:
            return False
        if any(marker in text for marker in (
            " compare ",
            " summarize ",
            " summarise ",
            " research ",
            " explain ",
            " send ",
            " share ",
            " with ",
            " then ",
            " and ",
            " after ",
            " before ",
        )):
            return False
        if any(marker in text for marker in (" use the link ", " from clipboard ", " selected text ", " clipboard ")):
            return False
        media_markers = (" video ", " clip ", " movie ", " song ", " music ", " playlist ", " podcast ", " album ", " youtube ")
        if intent.target_type == TargetType.URL and intent.target_value:
            target_value = intent.target_value.lower()
            return "youtube" in target_value or "youtu.be" in target_value
        return any(marker in text for marker in media_markers)

    def _derive_media_query(self, user_request: str) -> str:
        query = (user_request or "").strip()
        lowered = query.lower()
        prefixes = [
            "please ",
            "can you ",
            "could you ",
            "would you ",
            "will you ",
            "open ",
            "play ",
            "show ",
            "start ",
            "launch ",
        ]
        changed = True
        while changed:
            changed = False
            for prefix in prefixes:
                if lowered.startswith(prefix):
                    query = query[len(prefix):].strip()
                    lowered = query.lower()
                    changed = True
        for article in ("a ", "an ", "the "):
            if lowered.startswith(article):
                query = query[len(article):].strip()
                lowered = query.lower()
                break
        return query.strip(" .?!")

    def _build_media_open_shortcut(
        self,
        user_request: str,
        intent: UserIntent,
        available_tools: Optional[List[str]] = None,
    ) -> Optional[MilestonePlan]:
        if intent.ambiguous or not self._looks_like_simple_media_open(user_request, intent):
            return None

        allowed = set(available_tools or [])
        if intent.target_type == TargetType.URL and intent.target_value and "open_url" in allowed:
            target_url = intent.target_value if intent.target_value.startswith(("http://", "https://")) else f"https://{intent.target_value}"
            return MilestonePlan(
                task_summary=f"Open {target_url}",
                milestones=[
                    Milestone(
                        id=1,
                        goal=f"Open {target_url}",
                        success_signal="Requested media page is open",
                        hint_tools=["open_url"],
                        deliverable_key="opened_media",
                    )
                ],
                final_response=f"Opened {target_url}.",
                source="milestone_media_shortcut",
            )

        if "play_media" not in allowed:
            return None

        media_query = self._derive_media_query(user_request) or "funny video"
        return MilestonePlan(
            task_summary=f"Open media results for {media_query}",
            milestones=[
                Milestone(
                    id=1,
                    goal=f"Open YouTube results for {media_query}",
                    success_signal=f"YouTube search results for {media_query} are open",
                    hint_tools=["play_media"],
                    deliverable_key="opened_media",
                )
            ],
            final_response=f"Opened YouTube results for {media_query}.",
            source="milestone_media_shortcut",
        )

    def _looks_like_repeat_message_request(
        self,
        user_request: str,
        intent: UserIntent,
        world_state: WorldState,
    ) -> bool:
        if intent.action != IntentAction.COMMUNICATE:
            return False
        text = f" {_norm_text(user_request)} "
        repeat_markers = (" again ", " resend ", " repeat ", " same message ", " send it again ", " say it again ")
        if not any(marker in text for marker in repeat_markers):
            return False
        app_context = _norm_text(f"{world_state.active_app} {intent.target_value}")
        return any(marker in app_context for marker in ("whatsapp", "messages", "imessage", "slack", "discord", "telegram", "signal", "messenger"))

    def _build_repeat_message_shortcut(
        self,
        user_request: str,
        intent: UserIntent,
        world_state: WorldState,
        available_tools: Optional[List[str]] = None,
    ) -> Optional[MilestonePlan]:
        if intent.ambiguous or not self._looks_like_repeat_message_request(user_request, intent, world_state):
            return None

        allowed = set(available_tools or [])
        if not {"type_text", "press_key"}.issubset(allowed):
            return None

        hint_tools = [tool for tool in ("type_in_field", "type_text", "press_key") if tool in allowed]
        app_name = world_state.active_app or "the active chat app"
        return MilestonePlan(
            task_summary=f"Send the previous message again in {app_name}",
            milestones=[
                Milestone(
                    id=1,
                    goal=f"Send the previous message again in {app_name}",
                    success_signal="The previous message text is entered into the chat and sent",
                    hint_tools=hint_tools,
                    deliverable_key="message_sent",
                )
            ],
            final_response="Repeated the previous message.",
            source="milestone_repeat_message_shortcut",
        )

    async def create_plan(
        self,
        user_request: str,
        world_state: WorldState,
        available_tools: Optional[List[str]] = None,
        conversation_history: Optional[List[dict]] = None
    ) -> MilestonePlan:
        """Compatibility wrapper: all requests now plan through MilestonePlan."""
        t_start = time.time()

        intent = self.intent_parser.parse(user_request, world_state)
        world_state.intent = intent
        task_graph = self.intent_parser.extract_task_graph(user_request, world_state)
        world_state.task_graph = task_graph

        print(f"[Planner] Intent: {intent.action.value} → {intent.target_type.value}:{intent.target_value} (conf={intent.confidence:.0%})")
        print(f"[Planner] Task graph: entities={len(task_graph.entities)} selectors={len(task_graph.selectors)} complexity={task_graph.complexity_score:.1f}")

        if intent.ambiguous:
            print(f"[Planner] Ambiguous request, needs clarification")
            return MilestonePlan(
                task_summary=user_request,
                needs_clarification=True,
                clarification_prompt=intent.clarification_prompt,
                source="milestone_planner",
            )

        safety_prompt = self._hard_safety_clarification_prompt(intent)
        if safety_prompt:
            print(f"[Planner] Hard safety clarification ({time.time() - t_start:.2f}s)")
            return MilestonePlan(
                task_summary=user_request,
                needs_clarification=True,
                clarification_prompt=safety_prompt,
                source="milestone_planner",
            )

        repeat_message_shortcut = self._build_repeat_message_shortcut(user_request, intent, world_state, available_tools)
        if repeat_message_shortcut is not None:
            print(f"[Planner] Repeat message shortcut ({time.time() - t_start:.2f}s)")
            return repeat_message_shortcut

        media_shortcut = self._build_media_open_shortcut(user_request, intent, available_tools)
        if media_shortcut is not None:
            print(f"[Planner] Direct media shortcut ({time.time() - t_start:.2f}s)")
            return media_shortcut

        skill_candidates = self.template_registry.get_skill_candidates(
            user_request=user_request,
            intent=intent,
            world_state=world_state,
            available_tools=available_tools,
            limit=3,
        )
        skill_context = self.template_registry.format_skill_context(skill_candidates)
        skill_names = self.template_registry.skill_names(skill_candidates)
        if skill_candidates:
            print(f"[Planner] Active skill overlays: {', '.join(skill_names)}")

        milestone_plan = await self.create_milestone_plan(
            user_request=user_request,
            world_state=world_state,
            conversation_history=conversation_history,
            available_tools=available_tools,
            skill_context=skill_context,
            skill_names=skill_names,
        )
        if milestone_plan is None:
            return MilestonePlan(
                task_summary=user_request,
                needs_clarification=True,
                clarification_prompt=self.planning_failure_message(),
                source="milestone_planner",
            )

        elapsed = time.time() - t_start
        print(f"[Planner] Unified milestone plan ready ({elapsed:.2f}s)")
        return milestone_plan

    # ═══════════════════════════════════════════════════════════════
    #  Milestone Planning (V3)
    # ═══════════════════════════════════════════════════════════════

    async def create_milestone_plan(
        self,
        user_request: str,
        world_state: WorldState,
        conversation_history: Optional[List[dict]] = None,
        available_tools: Optional[List[str]] = None,
        skill_context: str = "",
        skill_names: Optional[List[str]] = None,
    ) -> Optional[MilestonePlan]:
        """
        Generate a milestone-based plan for a request.

        Returns None if no LLM provider is available or the model does not
        return usable plan JSON. Callers can then apply a deterministic
        milestone fallback or request clarification.
        """
        intent = world_state.intent or self.intent_parser.parse(user_request, world_state)
        repeat_message_shortcut = self._build_repeat_message_shortcut(user_request, intent, world_state, available_tools)
        if repeat_message_shortcut is not None:
            return repeat_message_shortcut

        media_shortcut = self._build_media_open_shortcut(user_request, intent, available_tools)
        if media_shortcut is not None:
            return media_shortcut

        if not self.provider:
            return None

        t_start = time.time()

        if not skill_context:
            candidates = self.template_registry.get_skill_candidates(
                user_request=user_request,
                intent=intent,
                world_state=world_state,
                available_tools=available_tools,
                limit=3,
            )
            skill_context = self.template_registry.format_skill_context(candidates)
            if skill_names is None:
                skill_names = self.template_registry.skill_names(candidates)
        if skill_names:
            print(f"[Planner] Milestone skill overlays: {', '.join(skill_names)}")

        # Build the filtered LLM-facing tool surface for planning.
        tool_categories = self._get_tool_category_summary(available_tools)

        # Conversation context
        conversation_context = "(none)"
        if conversation_history and len(conversation_history) > 1:
            parts = []
            for turn in conversation_history[:-1]:
                role = turn.get("role", "")
                text = ""
                for part in turn.get("parts", []):
                    if "text" in part:
                        text = part["text"]
                        break
                if text and role in ("user", "model"):
                    label = "User" if role == "user" else "Assistant"
                    parts.append(f"  {label}: {text[:200]}")
            if parts:
                conversation_context = "\n".join(parts)

        prompt = MILESTONE_PLANNING_PROMPT.format(
            tool_categories=tool_categories,
            world_state=world_state.to_prompt_string(),
            user_request=user_request,
            conversation_context=conversation_context,
            skill_context=skill_context or "(none)",
        )

        self.last_provider_error = None
        try:
            response = await self.provider.generate(
                messages=[{"role": "user", "parts": [{"text": prompt}]}],
                system_prompt=MILESTONE_PLANNING_SYSTEM,
                tools=[],
                temperature=0.15,
            )
            # Keep *why* planning failed. Without this a quota error reaches the
            # user as "try rephrasing", which sends them rewriting a fine request.
            if response is not None and response.provider_error is not None:
                self.last_provider_error = response.provider_error
                print(f"[Planner] ⚠ Planning call failed: {response.provider_error.log_line()}")
                return None
            if not response or not response.text:
                return None

            plan = self._parse_milestone_response(response.text, user_request)
            plan.skill_context = skill_context or ""
            plan.skills_used = list(skill_names or [])
            elapsed = time.time() - t_start
            print(f"[Planner] 🎯 Milestone plan generated ({elapsed:.2f}s, "
                  f"{len(plan.milestones)} milestones)")
            return plan

        except Exception as e:
            failure = classify_provider_error(e, requested_model=getattr(self.provider, "model", ""))
            self.last_provider_error = failure
            print(f"[Planner] ⚠ Milestone planning failed: {failure.log_line()}")
            return None

    def planning_failure_message(self) -> str:
        """
        Why the last planning attempt failed, phrased for the user.

        Falls back to the generic prompt only when the model genuinely returned
        something unusable, rather than hiding an infrastructure failure behind it.
        """
        if self.last_provider_error is not None:
            return self.last_provider_error.user_message()
        return "I couldn't generate a milestone plan for that. Please try rephrasing it."

    def _parse_milestone_response(self, text: str, user_request: str) -> MilestonePlan:
        """Parse an LLM milestone plan response into a MilestonePlan."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            first_nl = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_nl + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(cleaned[start:end])
            else:
                raise ValueError("Could not parse milestone plan JSON")

        if data.get("needs_clarification"):
            return MilestonePlan(
                task_summary=data.get("task_summary", user_request),
                needs_clarification=True,
                clarification_prompt=data.get("clarification_prompt", ""),
            )

        milestones = []
        for m_data in data.get("milestones", []):
            raw_hints = m_data.get("hint_tools", [])
            sanitized_hints = self._sanitize_hint_tools(raw_hints)
            milestones.append(Milestone(
                id=m_data.get("id", len(milestones) + 1),
                goal=m_data.get("goal", ""),
                success_signal=m_data.get("success_signal", ""),
                hint_tools=sanitized_hints,
                depends_on=m_data.get("depends_on", []),
                deliverable_key=m_data.get("deliverable_key", ""),
            ))

        return MilestonePlan(
            task_summary=data.get("task_summary", user_request),
            milestones=milestones,
            final_response=data.get("final_response", "Done!"),
            source="milestone_planner",
        )

    # Map commonly hallucinated tool names to their real equivalents
    _HALLUCINATED_TOOL_MAP: dict[str, list[str]] = {
        "computer_interface": ["click_ui", "type_in_field", "get_ui_tree"],
        "computer_control": ["click_ui", "type_in_field", "type_text", "press_key"],
        "computer_interaction": ["click_ui", "type_in_field", "type_text"],
        "open_application": ["open_app"],
        "close_application": ["quit_app"],
        "bash": ["run_shell"],
        "terminal": ["run_shell"],
        "shell": ["run_shell"],
        "browser": ["open_url", "browser_read_page"],
        "screenshot": ["read_screen"],
        "search": ["get_web_information"],
        "web_browse": ["open_url", "get_web_information"],
        "keyboard": ["press_key", "type_text"],
        "mouse": ["click_ui"],
        "ui_interact": ["click_ui", "type_in_field"],
        "desktop_interact": ["click_ui", "type_in_field", "get_ui_tree"],
    }

    def _sanitize_hint_tools(self, raw_hints: list) -> list[str]:
        """Map hallucinated tool names to real ones and drop unknowns."""
        available = set()
        if self.tool_registry:
            try:
                available = {d["name"] for d in self.tool_registry.declarations()}
            except Exception:
                pass

        sanitized: list[str] = []
        seen: set[str] = set()
        for hint in raw_hints or []:
            tool_name = str(hint).strip()
            if not tool_name:
                continue
            # Already a valid tool
            if not available or tool_name in available:
                if tool_name not in seen:
                    sanitized.append(tool_name)
                    seen.add(tool_name)
                continue
            # Try mapping hallucinated name → real tools
            mapped = self._HALLUCINATED_TOOL_MAP.get(tool_name.lower(), [])
            for real_tool in mapped:
                if (not available or real_tool in available) and real_tool not in seen:
                    sanitized.append(real_tool)
                    seen.add(real_tool)
            if not mapped:
                print(f"[Planner] ⚠ Dropping unknown hint_tool '{tool_name}'")
        return sanitized

    def _get_tool_category_summary(self, available_tools: Optional[List[str]] = None) -> str:
        """Build the filtered tool summary used by the milestone planner."""
        selector = get_tool_selector(self.tool_registry) if self.tool_registry else None
        if selector is None:
            return "  (no tools available)"
        return selector.format_planning_tool_summary(available_tools)

    def _is_compound_task_graph(self, task_graph: Optional[TaskGraph]) -> bool:
        if not task_graph:
            return False
        entity_types = task_graph.entity_types()
        if len(entity_types) >= 2:
            return True
        if len(task_graph.selectors) >= 1 and len(task_graph.entities) >= 1:
            return True
        if len(task_graph.desired_outcomes) >= 2:
            return True
        if task_graph.complexity_score >= 3.0:
            return True
        return False

    def _should_bypass_template_shortcuts(self, user_request: str, task_graph: Optional[TaskGraph]) -> bool:
        text = (user_request or "").lower()
        if self._is_compound_task_graph(task_graph):
            return True

        compound_markers = (
            " and ", " then ", " after ", " before ", " using ", " with ",
            " from ", " into ", " latest ", " newest ", " most recent ",
        )
        if any(marker in f" {text} " for marker in compound_markers):
            return True
        return False

    def should_use_milestones(
        self,
        user_request: str,
        task_summary: str = "",
        task_graph: Optional[TaskGraph] = None,
    ) -> bool:
        """Milestone planning is now universal for all requests."""
        return True

    def _hard_safety_clarification_prompt(self, intent: UserIntent) -> Optional[str]:
        """Return a clarification prompt for destructive requests, if needed."""
        text = (intent.raw_text or "").lower()
        dangerous_markers = (
            "rm -rf /",
            "delete production database",
            "drop all tables",
            "wipe my hard drive",
            "format my disk",
        )
        if any(marker in text for marker in dangerous_markers):
            return "This looks destructive and irreversible. I need explicit confirmation with exact scope before proceeding."
        return None

    def _is_research_document_request(self, user_request: str, task_summary: str) -> bool:
        text = f"{user_request or ''} {task_summary or ''}".lower()
        research_terms = ("research", "investigate", "analyze", "analyse", "compare", "study", "find", "look up", "best")
        document_terms = ("document", "report", "write up", "write-up", "brief", "paper", "article", "essay")
        return any(t in text for t in research_terms) and any(t in text for t in document_terms)

    def _is_general_research_request(self, user_request: str, task_summary: str) -> bool:
        text = f"{user_request or ''} {task_summary or ''}".lower()
        strong_markers = ("research", "investigate", "study", "analyze", "analyse", "compare")
        weak_markers = ("look up", "look-up", "find me", "find the best", "best", "top")
        return any(marker in text for marker in strong_markers) or any(marker in text for marker in weak_markers)

    def _build_sync_milestone_fallback(
        self,
        user_request: str,
        world_state: WorldState,
        intent: UserIntent,
        task_graph: Optional[TaskGraph] = None,
        *,
        skill_context: str = "",
        skill_names: Optional[List[str]] = None,
    ) -> MilestonePlan:
        """Deterministic milestone fallback for sync/LLM-unavailable callers."""
        skill_names = list(skill_names or [])

        if intent.ambiguous:
            return MilestonePlan(
                task_summary=user_request,
                needs_clarification=True,
                clarification_prompt=intent.clarification_prompt,
                skill_context=skill_context,
                skills_used=skill_names,
                source="milestone_sync_fallback",
            )

        if intent.action == IntentAction.OPEN and intent.target_type == TargetType.APP and intent.target_value:
            return MilestonePlan(
                task_summary=f"Open {intent.target_value}",
                milestones=[
                    Milestone(
                        id=1,
                        goal=f"Open {intent.target_value}",
                        success_signal=f"{intent.target_value} is open or focused",
                        hint_tools=["open_app"],
                        deliverable_key="app_opened",
                    )
                ],
                final_response=f"Opened {intent.target_value}.",
                skill_context=skill_context,
                skills_used=skill_names,
                source="milestone_sync_fallback",
            )

        if intent.action == IntentAction.OPEN and intent.target_type == TargetType.URL:
            target_url = intent.target_value or world_state.browser_url or ""
            if target_url and not target_url.startswith(("http://", "https://")):
                target_url = f"https://{target_url}"
            return MilestonePlan(
                task_summary=f"Open {target_url or 'the requested URL'}",
                milestones=[
                    Milestone(
                        id=1,
                        goal=f"Open {target_url or 'the requested URL'}",
                        success_signal="Requested page is open",
                        hint_tools=["open_url"],
                        deliverable_key="opened_url",
                    )
                ],
                final_response=f"Opened {target_url or 'the requested URL'}.",
                skill_context=skill_context,
                skills_used=skill_names,
                source="milestone_sync_fallback",
            )

        if intent.action == IntentAction.READ and intent.target_type == TargetType.FILE and intent.target_value:
            return MilestonePlan(
                task_summary=f"Read {intent.target_value}",
                milestones=[
                    Milestone(
                        id=1,
                        goal=f"Read {intent.target_value}",
                        success_signal="File contents are retrieved",
                        hint_tools=["read_file"],
                        deliverable_key="file_contents",
                    )
                ],
                final_response=f"Read {intent.target_value}.",
                skill_context=skill_context,
                skills_used=skill_names,
                source="milestone_sync_fallback",
            )

        if task_graph and self._is_compound_task_graph(task_graph):
            return MilestonePlan(
                task_summary=user_request,
                needs_clarification=True,
                clarification_prompt=(
                    "I need the milestone planner to break that down, but no async planning provider "
                    "is available in this sync path."
                ),
                skill_context=skill_context,
                skills_used=skill_names,
                source="milestone_sync_fallback",
            )

        return MilestonePlan(
            task_summary=user_request,
            needs_clarification=True,
            clarification_prompt="I'm not sure how to help with that. Could you be more specific?",
            skill_context=skill_context,
            skills_used=skill_names,
            source="milestone_sync_fallback",
        )

    # ═══════════════════════════════════════════════════════════════
    #  Dynamic Replanning
    # ═══════════════════════════════════════════════════════════════

    async def replan_remaining(
        self,
        plan: MilestonePlan,
        failed_milestone_id: int,
        failure_reason: str,
        deliverables: dict,
    ) -> list:
        """
        Re-plan the remaining milestones after a failure.

        Args:
            plan: The current MilestonePlan being executed.
            failed_milestone_id: ID of the milestone that failed.
            failure_reason: Description of why it failed.
            deliverables: Dict of milestone_id → deliverable string from completed milestones.

        Returns:
            A list of revised Milestone objects to replace the remaining milestones.
            Returns an empty list if replanning fails or the failure is unrecoverable.
        """
        completed_lines = []
        remaining_lines = []
        failed_goal = ""

        for m in plan.milestones:
            if m.status == MilestoneStatus.COMPLETED:
                completed_lines.append(
                    f"  M{m.id}: {m.goal} → ✅ {m.result_summary[:120]}"
                )
            elif m.id == failed_milestone_id:
                failed_goal = m.goal
            elif m.status == MilestoneStatus.PENDING:
                remaining_lines.append(
                    f"  M{m.id}: {m.goal} (depends_on={m.depends_on})"
                )

        deliverables_lines = []
        for mid, value in deliverables.items():
            deliverables_lines.append(f"  M{mid}: {str(value)[:200]}")

        next_id = max((m.id for m in plan.milestones), default=0) + 1

        prompt = REPLAN_PROMPT.format(
            failed_id=failed_milestone_id,
            user_request=plan.task_summary,
            task_summary=plan.task_summary,
            completed_summary="\n".join(completed_lines) or "(none)",
            failed_goal=failed_goal,
            failure_reason=failure_reason[:500],
            remaining_summary="\n".join(remaining_lines) or "(none)",
            deliverables_summary="\n".join(deliverables_lines) or "(none)",
            next_id=next_id,
        )

        # Try LLM replanning
        if self.provider:
            try:
                response = await self.provider.generate(
                    messages=[{"role": "user", "parts": [{"text": prompt}]}],
                    system_prompt=MILESTONE_PLANNING_SYSTEM,
                    tools=[],
                    temperature=0.2,
                )
                if response and response.text:
                    milestones = self._parse_replan_response(
                        response.text, next_id
                    )
                    if milestones is not None:
                        print(
                            f"[Planner] 🔄 Replanned: {len(milestones)} revised "
                            f"milestone(s) after M{failed_milestone_id} failure"
                        )
                        return milestones
            except Exception as e:
                print(f"[Planner] ⚠ LLM replanning failed: {e}")

        # Fallback: generate a single retry milestone
        print(f"[Planner] 🔄 Fallback replan: single retry milestone")
        return [
            Milestone(
                id=next_id,
                goal=f"Retry: {failed_goal}",
                success_signal=f"The goal '{failed_goal}' is achieved",
                hint_tools=[],
                deliverable_key=f"retry_{failed_milestone_id}",
            )
        ]

    def _parse_replan_response(
        self, text: str, start_id: int
    ) -> Optional[list]:
        """Parse the LLM replan response into a list of Milestone objects."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            first_nl = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_nl + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            print(f"[Planner] ⚠ Replan JSON parse failed")
            return None

        raw_milestones = data.get("milestones", [])
        if not isinstance(raw_milestones, list):
            return None

        recovery = data.get("recovery_strategy", "")
        if recovery:
            print(f"[Planner] 🔄 Recovery strategy: {recovery[:150]}")

        milestones = []
        for idx, m in enumerate(raw_milestones):
            if not isinstance(m, dict):
                continue
            milestones.append(
                Milestone(
                    id=m.get("id", start_id + idx),
                    goal=m.get("goal", "Unknown goal"),
                    success_signal=m.get("success_signal", ""),
                    hint_tools=m.get("hint_tools", []),
                    depends_on=m.get("depends_on", []),
                    deliverable_key=m.get("deliverable_key", ""),
                )
            )

        return milestones if milestones else None

    def create_plan_sync(
        self,
        user_request: str,
        world_state: WorldState
    ) -> MilestonePlan:
        """
        Synchronous compatibility planner.

        The sync path no longer emits ExecutionPlans or invokes template
        short-circuits. It returns a deterministic MilestonePlan fallback.
        """
        intent = self.intent_parser.parse(user_request, world_state)
        world_state.intent = intent
        task_graph = self.intent_parser.extract_task_graph(user_request, world_state)
        world_state.task_graph = task_graph

        safety_prompt = self._hard_safety_clarification_prompt(intent)
        if safety_prompt:
            return MilestonePlan(
                task_summary=user_request,
                needs_clarification=True,
                clarification_prompt=safety_prompt,
                source="milestone_sync_fallback",
            )

        skill_candidates = self.template_registry.get_skill_candidates(
            user_request=user_request,
            intent=intent,
            world_state=world_state,
            available_tools=None,
        )
        skill_context = self.template_registry.format_skill_context(skill_candidates)
        skill_names = self.template_registry.skill_names(skill_candidates)

        return self._build_sync_milestone_fallback(
            user_request=user_request,
            world_state=world_state,
            intent=intent,
            task_graph=task_graph,
            skill_context=skill_context if skill_context != "(none)" else "",
            skill_names=skill_names,
        )
