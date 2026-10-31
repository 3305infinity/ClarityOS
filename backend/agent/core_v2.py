"""
ClarityOS — Agent Core V2
=========================
Sense-Plan-Act-Verify (SPAV) architecture for improved tool calling.

Key improvements over V1:
  1. Structured WorldState instead of raw context strings
  2. Explicit milestone planning before execution
  3. Milestone micro-loop execution with verification
  4. Intelligent tool selection (reduced from 30+ to a focused request surface)
  5. Better error recovery with evidence-gated completion
"""

import asyncio
import json
import time as _time
import os
import uuid
import random as _random
import re as _re
from typing import Callable, Optional, Awaitable, List
from functools import partial
from dataclasses import dataclass, field
from urllib.parse import urlparse

print = partial(print, flush=True)

# Load .env file
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# ClarityOS V2 modules
from agent.world_state import WorldState, UserIntent, IntentParser, EntityExtractor, IntentAction
from agent.planner import ExecutionStep, StepStatus
from agent.planner import MilestonePlan, Milestone, MilestoneStatus
from agent.task_planner import TaskPlanner
from agent.direct_actions import match_direct_open, describe_direct_open
from agent.verifier import get_verifier
from agent.memory import ConversationMemory, UserPreferences, TaskStore, UserProfile, WorkingMemory, VaultMemory
from tools.selector import get_tool_selector, set_tool_gateway_context, set_web_progress_callback
from tools import registry as tool_registry
from providers.router import ModelRouter
from providers import LLMProvider
import agent.perception as perception


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

PENDING_PLAN_TTL_SECONDS = 600

# Browser timing — seconds to wait after navigation actions so the page
# has time to load and the snapshot can be refreshed.  Centralised here
# instead of scattered as magic numbers throughout _execute_step().
BROWSER_NAV_SETTLE_S = 0.3      # reduced — snapshot event-wait handles the real delay
BROWSER_RECOVERY_SETTLE_S = 0.2 # after refresh_refs recovery

BROWSER_APP_NAMES = {
    "google chrome",
    "chrome",
    "safari",
    "arc",
    "brave browser",
    "firefox",
    "microsoft edge",
    "edge",
}

WEB_FLOW_TOOLS = {
    "open_url",
    "get_web_information",
    "web_search",
    "fetch_web_content",
    "web_scrape",
    "browser_snapshot",
    "browser_read_page",
    "browser_read_text",
    "read_page_content",
    "extract_structured_data",
    "get_page_summary",
    "browser_find",
    "browser_click_match",
    "browser_click_ref",
    "browser_type_ref",
    "browser_select_ref",
    "browser_refresh_refs",
    "browser_scroll",
    "browser_wait_for",
    "browser_list_tabs",
    "browser_switch_tab",
    "gdocs_create",
    "gdocs_append",
    "gdocs_read",
    "gworkspace_analyze",
}

# Simplified system prompt for V2 (planning is done separately)
SYSTEM_PROMPT_V2 = """You are ClarityOS, a macOS desktop assistant.

## Personality & Tone
- Warm, helpful, and efficient — like a capable friend, not a corporate bot.
- Keep responses concise (1-3 sentences for simple tasks). Be direct.
- Occasionally use light humor or personality, but never force it.
- Use the user's name naturally when you know it (from profile).
- Match the user's energy — enthusiastic with excited users, calm with
  frustrated ones, casual with casual tone, professional when appropriate.
- When something goes wrong, be empathetic and solution-focused.
- Celebrate successes briefly ("Done!", "All set!") but don't be over-the-top.
- Remember: you're voice-first. Write responses that sound natural when spoken aloud.

## Reasoning Protocol (MANDATORY)
Before EVERY tool call you MUST include a `reasoning` argument — one sentence
explaining WHY you are calling that tool and what you expect the result to be.

<thought>
Before calling any tool, think step-by-step:
1. What is my current goal?
2. What do I know from previous tool results?
3. What is the best next action and why?
</thought>

You MUST output a <thought> block before each tool call in your response.
The reasoning argument is your external justification; the thought block is
your internal chain-of-thought.

Core behavior:
- Requests are voice-transcribed; correct obvious transcription errors.
- Resolve references ("it", "that", "go ahead") from conversation history + desktop context.
- Prefer action over clarification when context resolves ambiguity.

Tooling rules:
- App/URL control: `open_app`, `open_url`, `quit_app`, `close_window`.
- UI interaction: prefer `click_ui` and `type_in_field`.
- Browser interaction: use extension tools (`browser_read_page`, `browser_click_match`, `browser_click_ref`, `browser_type_ref`, `browser_select_ref`).
- Browser scrolling: use `browser_scroll`, not `press_key`.
- Use `run_shell` only for real shell/file/system tasks, never for browser/UI automation.
- For file edits: read first (`read_file`), then mutate (`replace_in_file`/`write_file`).
- For Google Docs/Sheets/Slides/Gmail/Calendar, prefer dedicated `g*` API tools.

Communication rules:
- `send_response` is the final user message for completed work.
- `await_reply` is only for a true blocking question.
- Do not emit raw conversational text outside these tools.
"""

# WebSocket callback type
WSCallback = Callable[[dict], Awaitable[None]]


@dataclass
class PendingPlanState:
    """Frozen plan awaiting explicit user approval."""
    plan_id: str
    plan: MilestonePlan
    created_at: float
    context_fingerprint: dict
    provider: Optional[LLMProvider]
    original_user_request: str
    selected_tools: List[str] = field(default_factory=list)
    stale_replan_count: int = 0


@dataclass
class PendingExecutionState:
    """Suspended milestone execution awaiting user follow-up."""
    execution_id: str
    plan: MilestonePlan
    created_at: float
    provider: Optional[LLMProvider]
    original_user_request: str
    selected_tools: List[str] = field(default_factory=list)
    deliverables: dict[int, str] = field(default_factory=dict)
    milestone_step_results: dict[int, str] = field(default_factory=dict)
    milestone_step_result_idx: int = 0
    suspended_milestone_id: int = 0
    await_payload: dict = field(default_factory=dict)
    followup_inputs: List[str] = field(default_factory=list)
    blocked_await_signatures: List[str] = field(default_factory=list)
    last_opened_url: str = ""


# ═══════════════════════════════════════════════════════════════
#  Agent V2 Class
# ═══════════════════════════════════════════════════════════════

class ClarityOSAgentV2:
    """
    V2 Agent with Sense-Plan-Act-Verify architecture.
    
    Flow:
    1. SENSE: Build structured WorldState from context
    2. PLAN: Generate a milestone plan
    3. ACT: Execute milestones via the LLM micro-loop
    4. VERIFY: Check success and handle failures
    """

    def __init__(self, use_planning: bool = True, persist: bool = True):
        """
        Initialize the V2 agent.
        
        Args:
            use_planning: If True, use LLM for complex planning. 
                         If False, use only rule-based templates (faster).
            persist: If True, persist conversation sessions to disk.
                    Set False for benchmarks / tests.
        """
        # Memory systems
        self.conversation = ConversationMemory(max_turns=30, idle_timeout=600, persist=persist)
        self.preferences = UserPreferences()
        self.user_profile = UserProfile()
        self.task_store = TaskStore()
        self.working_memory = WorkingMemory(max_actions=40, max_entities=60)
        self.vault = VaultMemory()

        # Retrieval verdicts worth telling the user about: content that was
        # usable but came from a different surface than the request implied.
        self._partial_retrieval_notes: list[str] = []
        self._partial_note_emitted = False
        self._last_answer_urls: list[str] = []
        self._current_context = None
        self._binding_problem = ""

        # Routing
        self.router = ModelRouter()
        self._pending_reply_provider: Optional[LLMProvider] = None
        
        # V2 components
        self.intent_parser = IntentParser()
        self.entity_extractor = EntityExtractor()
        self.tool_selector = get_tool_selector(tool_registry)
        self.verifier = get_verifier()
        self.planner: Optional[TaskPlanner] = None  # Lazy init with provider
        self._pending_plan: Optional[PendingPlanState] = None
        self._pending_execution: Optional[PendingExecutionState] = None
        self._last_opened_url: str = ""
        
        # Configuration
        self.use_planning = use_planning

    def _build_system_prompt(self) -> str:
        """Build the system prompt with user preferences, profile, and working memory."""
        prompt = SYSTEM_PROMPT_V2
        prefs = self.preferences.to_prompt_string()
        if prefs:
            prompt += f"\n\n{prefs}"
        profile = self.user_profile.to_prompt_string()
        if profile:
            prompt += f"\n\n{profile}"
        # Inject working memory (recent actions, entities, session goal)
        wm = self.working_memory.to_prompt_string()
        if wm:
            prompt += f"\n\n{wm}"
        # Inject vault memory summary (permanent cross-session knowledge)
        vault_summary = self.vault.to_prompt_string()
        if vault_summary:
            prompt += f"\n\n{vault_summary}"
        return prompt

    def _context_fingerprint(self, context: perception.ContextSnapshot) -> dict:
        app = (context.active_app or "").strip().lower()
        domain = ""
        if context.browser_url:
            try:
                domain = (urlparse(context.browser_url).netloc or "").lower()
            except Exception:
                domain = ""
        return {
            "active_app": app,
            "browser_domain": domain,
        }

    def _classify_pending_followup(self, text: str) -> str:
        normalized = " ".join((text or "").strip().lower().split())
        if not normalized:
            return "modify"
        import re

        def _has_term(term: str) -> bool:
            if " " in term:
                return term in normalized
            return re.search(rf"\b{re.escape(term)}\b", normalized) is not None

        cancel_terms = (
            "cancel",
            "never mind",
            "nevermind",
            "stop",
            "abort",
            "dismiss",
            "don't",
            "do not",
            "no thanks",
            "no",
        )
        modify_terms = (
            "change",
            "modify",
            "instead",
            "but",
            "except",
            "skip",
            "remove",
            "add",
            "edit",
            "update",
        )
        approve_terms = (
            "proceed",
            "go ahead",
            "yes",
            "approved",
            "approve",
            "start",
            "continue",
            "do it",
            "looks good",
            "ok",
            "okay",
        )
        # Asking to SEE the plan is not asking to change it. "where is the
        # plan?" fell through to "modify", which regenerated the plan and
        # re-sent the same approval prompt — so the question looked unanswered
        # and the user asked again, forever.
        show_terms = (
            "where is the plan", "where's the plan", "wheres the plan",
            "show me the plan", "show the plan", "what is the plan",
            "what's the plan", "whats the plan", "see the plan",
            "which plan", "no plan", "didn't show", "didnt show",
            "cant see", "can't see", "not showing", "nothing happened",
        )
        if any(_has_term(term) for term in show_terms):
            return "show"
        if any(_has_term(term) for term in cancel_terms):
            return "cancel"
        if any(_has_term(term) for term in modify_terms):
            return "modify"
        if any(_has_term(term) for term in approve_terms):
            return "approve"
        return "modify"

    # ── Instant Acknowledgment ──────────────────────────────────

    _ACK_ACTION = [
        "On it!", "Sure thing", "Got it", "Working on it",
        "One sec", "Right away", "Let me handle that",
    ]
    _ACK_QUESTION = [
        "Let me check", "Good question", "Looking into it",
        "One moment", "Let me find out",
    ]
    _ACK_RESEARCH = [
        "Let me look into that", "Searching now",
        "I'll dig into that", "Researching",
    ]

    def _pick_ack(self, user_text: str) -> str:
        """Pick a natural acknowledgment phrase based on request type."""
        lower = user_text.strip().lower()
        # Questions
        if any(lower.startswith(w) for w in ("what", "how", "why", "who", "where", "when", "is ", "are ", "can ", "do ", "does ")):
            return _random.choice(self._ACK_QUESTION)
        # Research-like
        if any(w in lower for w in ("research", "find", "search", "look up", "compare")):
            return _random.choice(self._ACK_RESEARCH)
        # Default action
        return _random.choice(self._ACK_ACTION)

    # ── Small-Talk Fast Path ────────────────────────────────────

    _GREETING_PATTERNS = _re.compile(
        r"^\s*(?:hey|hi|hello|good\s+(?:morning|afternoon|evening)|howdy|sup|what'?s\s+up|yo)\s*[!.?]?\s*$",
        _re.IGNORECASE,
    )
    _THANKS_PATTERNS = _re.compile(
        r"^\s*(?:thanks?(?:\s+you)?|thank\s+you(?:\s+so\s+much)?|thx|cheers|appreciate\s+it)\s*[!.?]?\s*$",
        _re.IGNORECASE,
    )
    _FAREWELL_PATTERNS = _re.compile(
        r"^\s*(?:bye|goodbye|see\s+you|later|good\s*night|take\s+care|peace)\s*[!.?]?\s*$",
        _re.IGNORECASE,
    )
    _MOOD_PATTERNS = _re.compile(
        r"^\s*(?:how\s+are\s+you|how'?s\s+it\s+going|how\s+do\s+you\s+feel|are\s+you\s+there)\s*[?!.]?\s*$",
        _re.IGNORECASE,
    )
    _SIMPLE_FACTUAL = _re.compile(
        r"^\s*(?:what(?:'s| is)\s+the\s+(?:time|date|day)|what\s+time\s+is\s+it|what\s+day\s+is\s+(?:it|today))\s*[?]?\s*$",
        _re.IGNORECASE,
    )

    # Self-contained arithmetic needs no screen, window or page context, yet it
    # was going through planner + milestone executor — two model round-trips and
    # ~29s for "29 times 13". Deliberately narrow: a digit, an arithmetic cue,
    # and no word that could refer to something on the user's machine. Anything
    # that might need grounding keeps the full pipeline.
    _ARITHMETIC_CUE = _re.compile(
        r"(?:[-+*/×÷%^]|\b(?:times|plus|minus|divided\s+by|multiplied\s+by|"
        r"squared|cubed|percent|of)\b)", _re.IGNORECASE)
    _CONTEXT_WORDS = _re.compile(
        r"\b(?:screen|page|tab|browser|window|file|code|app|application|this|that|"
        r"here|visible|open|selected|clipboard|chrome|vs\s*code|notepad)\b", _re.IGNORECASE)

    def _is_pure_computation(self, text: str) -> bool:
        probe = (text or "").strip()
        if not probe or len(probe) > 120:
            return False
        if not _re.search(r"\d", probe):
            return False
        if self._CONTEXT_WORDS.search(probe):
            return False
        return bool(self._ARITHMETIC_CUE.search(probe))

    def _is_conversational(self, text: str) -> str | None:
        """
        Classify if text is small-talk that can skip the full pipeline.
        Returns the category string or None if it's a real task.
        """
        if self._GREETING_PATTERNS.match(text):
            return "greeting"
        if self._THANKS_PATTERNS.match(text):
            return "thanks"
        if self._FAREWELL_PATTERNS.match(text):
            return "farewell"
        if self._MOOD_PATTERNS.match(text):
            return "mood"
        if self._SIMPLE_FACTUAL.match(text):
            return "factual"
        if self._is_pure_computation(text):
            return "factual"
        return None

    async def _try_conversational_fast_path(
        self,
        user_text: str,
        context: perception.ContextSnapshot,
        ws_callback: Optional[WSCallback] = None,
    ) -> tuple | None:
        """
        Handle greetings, thanks, farewells, and simple questions
        without going through the full routing/planning pipeline.
        Returns (response_text, False) or None if not conversational.
        """
        category = self._is_conversational(user_text)
        if not category:
            return None

        print(f"[AgentV2] 💬 Conversational fast path: {category}")

        # Get user's name for personalization
        user_name = self.user_profile.get_fact("name") or ""

        # Use Flash for speed. ModelRouter exposes `.fast` / `.fallback`
        # properties — there is no `.get()`. Calling it raised AttributeError
        # for every request that reached the conversational fast path, so the
        # path never answered: it threw and the task ended with no response
        # event at all, which the UI showed as a hang.
        provider = self.router.fast or self.router.fallback
        if not provider:
            # Fallback to template responses
            return self._template_chat_response(category, user_name)

        # Build a lightweight conversational prompt
        history = self.conversation.get_history()[-6:]
        history_text = ""
        for turn in history:
            role = turn.get("role", "")
            parts = turn.get("parts", [])
            text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if text:
                history_text += f"{'User' if role == 'user' else 'ClarityOS'}: {text}\n"

        profile_hint = ""
        if user_name:
            profile_hint = f"The user's name is {user_name}. Use it naturally (not every time).\n"

        system = (
            "You are ClarityOS, a friendly macOS desktop assistant. "
            "Keep responses short (1-2 sentences max), natural, and warm. "
            "Sound like a helpful friend, not a corporate bot. "
            "You're voice-first — write responses that sound natural when spoken aloud. "
            f"{profile_hint}"
        )

        prompt = f"Conversation so far:\n{history_text}\nUser: {user_text}\n\nRespond naturally:"

        try:
            # GeminiProvider exposes `generate`, not `chat`. The wrong name
            # meant this always raised and silently degraded to a canned
            # template, which is why conversational replies felt generic.
            response = await provider.generate(
                system_prompt=system,
                messages=[{"role": "user", "parts": [{"text": prompt}]}],
                tools=[],
                temperature=0.7,
            )
            # `generate` returns an LLMResponse dataclass, not a dict.
            reply = (getattr(response, "text", "") or "").strip()

            # A quota/auth failure must not be dressed up as chit-chat. The
            # template ("Let me check that for you…") implied work was under
            # way when the model had refused the call outright.
            provider_error = getattr(response, "provider_error", None)
            if not reply and provider_error is not None:
                message = provider_error.user_message()
                self.conversation.add_model(message)
                return (message, False)

            if not reply:
                return self._template_chat_response(category, user_name)
        except Exception as e:
            print(f"[AgentV2] Fast path LLM error: {e}")
            return self._template_chat_response(category, user_name)

        # Deliver the response
        if ws_callback:
            display = "pill" if len(reply) <= 60 else "card"
            try:
                await tool_registry.execute("send_response", {"message": reply})
            except Exception:
                pass
            await ws_callback({
                "type": "response",
                "payload": {
                    "text": reply,
                    "display": display,
                    "app": context.active_app.lower() if context.active_app else "",
                },
            })

        self.conversation.add_model(reply)
        return (reply, False)

    # ── Deterministic Windows Actions ───────────────────────────
    #
    # Runs before routing and planning. "open notepad" is fully determined by the
    # request, so spending three LLM calls on it is pure latency — and makes a
    # local, offline-capable action fail whenever the model is out of quota.

    async def _try_direct_windows_action(self, user_text: str, ws_callback=None) -> Optional[tuple]:
        """
        Handle an unambiguous "open <known app>" request without the LLM.

        Returns None for anything the closed vocabulary doesn't cover, so the
        normal planning pipeline still sees the request untouched.
        """
        app_name = match_direct_open(user_text)
        if app_name is None:
            return None
        if "open_app" not in tool_registry.list_names():
            return None     # helper not connected / tool group not allowed

        print(f"[AgentV2] 🎯 Direct action: open_app({app_name})")
        if ws_callback:
            await ws_callback({"type": "doing", "text": f"Opening {app_name}…", "tool": "open_app"})

        # Canonical name from the fixed table — never the user's raw text.
        result = await tool_registry.execute("open_app", {"app_name": app_name})
        result_text = str(result or "").strip()

        if result_text.startswith("ERROR") or '"ok": false' in result_text.lower():
            # Hand the real tool failure back rather than a planning error.
            message = self._direct_action_failure(app_name, result_text)
            print(f"[AgentV2] Direct action failed: {result_text[:200]}")
            if ws_callback:
                await ws_callback({"type": "response", "payload": {"text": message}})
            self.conversation.add_model(message)
            return (message, False)

        # open_app only reports "Opened X (now in front)" once it has actually
        # seen the window take focus. Its other success string means the launch
        # was fired but never confirmed — reporting that as "Opened Spotify."
        # is how the agent ended up claiming success for an app that never
        # started. Say what was actually observed instead.
        if "may still be loading" in result_text.lower():
            # A cold start (Spotify, Teams, VS Code…) routinely takes longer
            # than open_app's 3s foreground wait, so this string means "not
            # confirmed yet", not "failed". _confirm_app_running polls the
            # process list to settle it — it existed for exactly this and was
            # never actually called, so every cold start was reported as
            # "it may take a few more seconds", including the ones already on
            # screen. Ask the OS instead of hedging.
            if await self._confirm_app_running(app_name, attempts=4, delay=1.0):
                print(f"[AgentV2] Direct action confirmed running: {app_name}")
            else:
                message = (f"I started {app_name} — it was still loading, so it may take "
                           f"a few more seconds to appear. Tell me if it doesn't open.")
                print(f"[AgentV2] Direct action launched but unconfirmed: {result_text[:200]}")
                if ws_callback:
                    await ws_callback({
                        "type": "response",
                        "payload": {"text": message, "display": "pill", "app": app_name.lower()},
                    })
                self.conversation.add_model(message)
                return (message, False)

        message = describe_direct_open(app_name)
        if ws_callback:
            try:
                await tool_registry.execute("send_response", {"message": message})
            except Exception:
                pass
            await ws_callback({
                "type": "response",
                "payload": {"text": message, "display": "pill", "app": app_name.lower()},
            })
        self.conversation.add_model(message)
        return (message, False)

    async def _confirm_app_running(self, app_name: str, attempts: int = 4, delay: float = 1.5) -> bool:
        """
        Did the app actually start? Polls the running-process list rather than
        trusting that a launch command returned without raising.
        """
        import asyncio as _asyncio

        # `re` is imported as `_re` in this module — this line raised NameError
        # on every call, which nothing noticed because the helper was never
        # wired up to a caller.
        tokens = [t for t in _re.split(r"\W+", app_name.lower()) if len(t) > 2] or [app_name.lower()]
        for _ in range(max(1, attempts)):
            await _asyncio.sleep(delay)
            try:
                # Bounded: this check must never sit waiting on an approval
                # prompt or a slow helper call and stall the user's answer.
                running = str(await _asyncio.wait_for(
                    tool_registry.execute("get_running_apps", {}), 4.0) or "").lower()
            except Exception:
                return False
            if running.startswith("error"):
                return False
            if any(token in running for token in tokens):
                return True
        return False

    # ── LangGraph orchestration (opt-in) ────────────────────────
    async def _try_langgraph(self, user_text: str, context, ws_callback=None) -> Optional[tuple]:
        """
        Run this request through the LangGraph workflow, or return None to let
        the original path handle it.

        Returns None (never raises) when the flag is off, the package is
        missing, or the request type isn't supported by the graph yet. A graph
        failure *before* execution also falls back; a failure after execution
        does not, because the plan has already had real-world effects.
        """
        from agent import graph_agent

        if not graph_agent.is_enabled():
            return None
        if not graph_agent.is_available():
            print("[AgentV2] CLARITYOS_USE_LANGGRAPH is set but langgraph is not installed — "
                  "using the original agent path")
            return None

        print("[AgentV2] → LangGraph ENABLED — routing this request through the graph")
        await self.router.initialize()
        try:
            # The graph's respond node only *returns* its text. When the graph
            # answers without executing (clarification, provider error), nothing
            # emits a `response` event, so the reply was spoken by TTS but never
            # rendered and the UI sat on "Planning…". When the graph *does*
            # execute, its send_response tool already emits one — so watch the
            # stream and only fill the gap, never duplicate it.
            emitted = {"response": False}

            async def watched_callback(msg: dict):
                if isinstance(msg, dict) and msg.get("type") == "response":
                    emitted["response"] = True
                await ws_callback(msg)

            cb = watched_callback if ws_callback else None
            result = await graph_agent.run_graph(self, user_text, context, cb)

            if ws_callback and not emitted["response"] and isinstance(result, tuple) and result:
                text = str(result[0] or "").strip()
                if text:
                    await ws_callback({"type": "response", "payload": {"text": text}})
            return result
        except Exception as e:
            # Safe fallback only while nothing has run. run_graph latches
            # `executed` before the side-effecting call, and re-raises nothing
            # after it, so reaching here means no tool has fired.
            print(f"[AgentV2] ⚠ Graph failed before execution ({e.__class__.__name__}: {e}) — "
                  f"falling back to the original path")
            return None

    @staticmethod
    def _direct_action_failure(app_name: str, result_text: str) -> str:
        """Turn an open_app tool error into an actionable sentence."""
        lowered = result_text.lower()
        if "not connected" in lowered or "helper" in lowered and "connect" in lowered:
            return (f"I can't open {app_name} because the Windows helper isn't connected. "
                    f"Start it with .\\windows_helper\\start.ps1 and try again.")
        if "no installed app" in lowered or "not found" in lowered:
            return (f"I couldn't find {app_name} installed on this PC.")
        detail = result_text[len("ERROR:"):].strip() if result_text.upper().startswith("ERROR:") else result_text
        return f"I couldn't open {app_name}: {detail[:160]}"

    def _template_chat_response(self, category: str, user_name: str = "") -> tuple:
        """Fallback template responses when no LLM is available."""
        name_suffix = f", {user_name}" if user_name else ""
        templates = {
            "greeting": [f"Hey{name_suffix}! What can I help with?", f"Hi{name_suffix}! Ready when you are.", "Hey! What's on your mind?"],
            "thanks": ["Happy to help!", "Anytime!", "You got it!"],
            "farewell": [f"See you{name_suffix}!", "Take care!", "Catch you later!"],
            "mood": ["I'm doing great, thanks for asking! What can I help with?", "All good here! What's up?"],
            "factual": ["Let me check that for you…"],
        }
        choices = templates.get(category, ["What can I help you with?"])
        reply = _random.choice(choices)
        self.conversation.add_model(reply)
        return (reply, False)

    def _suggest_followup(self, response: str, user_text: str) -> str:
        """Return a brief proactive follow-up, or empty string to skip."""
        suggestions = [
            "Anything else I can help with?",
            "Want me to do anything else with this?",
            "Need anything else?",
            "Let me know if you'd like to tweak anything.",
            "Want me to continue with something related?",
        ]
        # Don't suggest if the response already asks a question
        if response.rstrip().endswith("?"):
            return ""
        return _random.choice(suggestions)

    def _recent_conversation_snippet(self) -> str:
        recent_turns = self.conversation.get_history()[-6:]
        return " ".join(
            p.get("text", "") for t in recent_turns
            for p in (t.get("parts") or []) if isinstance(p, dict) and "text" in p
        )

    def _current_tool_gateway_context(self) -> dict:
        """Collect live browser bridge state for gateway routing."""
        try:
            from browser.bridge import browser_bridge
            from browser.store import browser_store
        except Exception:
            return {
                "browser_bridge_connected": False,
                "browser_has_snapshot": False,
                "browser_session_id": "",
            }

        snapshot = browser_store.get_snapshot()
        session_id = (
            str(browser_bridge.connected_session_id() or "").strip()
            or str(getattr(snapshot, "session_id", "") or "").strip()
        )
        return {
            "browser_bridge_connected": bool(browser_bridge.is_connected()),
            "browser_has_snapshot": snapshot is not None,
            "browser_session_id": session_id,
        }

    def _select_tool_surface(
        self,
        user_request: str,
        context: perception.ContextSnapshot,
        world_state: Optional[WorldState] = None,
    ) -> tuple[list[str], list[dict]]:
        intent = getattr(world_state, "intent", None) if world_state else None
        if intent is None or intent.action == IntentAction.UNKNOWN:
            try:
                intent = self.intent_parser.parse(user_request)
            except Exception:
                intent = None
        selected_tools = self.tool_selector.select(
            user_request=user_request,
            context_app=context.active_app or "",
            context_url=context.browser_url or "",
            conversation_history=self._recent_conversation_snippet(),
            clipboard_content=context.clipboard or "",
            selected_text=getattr(context, "selected_text", None) or "",
            intent_action=intent.action.value if intent else "",
            intent_target_type=intent.target_type.value if intent else "",
            intent_target_value=intent.target_value if intent else "",
        )
        llm_tool_declarations = self.tool_selector.get_llm_tool_declarations(selected_tools)
        return selected_tools, llm_tool_declarations

    def _llm_tool_names(self, llm_tool_declarations: Optional[list[dict]]) -> set[str]:
        return {
            str(decl.get("name", "")).strip()
            for decl in (llm_tool_declarations or [])
            if str(decl.get("name", "")).strip()
        }

    @staticmethod
    def _note_plan_approval(plan) -> None:
        """Tell the guard which tools the user just approved, via the plan.

        Scoped to the tools this plan names, and each grant is single-use, so
        approving one plan cannot authorise an unrelated action later. A
        capability switched off in Settings is still refused.
        """
        try:
            from servers.voice_guard import note_plan_approved
        except Exception:
            return
        tools: set = set()
        for milestone in getattr(plan, "milestones", []) or []:
            for tool in (getattr(milestone, "hint_tools", None) or []):
                name = str(tool).strip()
                if name:
                    tools.add(name)
        if tools:
            note_plan_approved(tools)

    @staticmethod
    def _clear_plan_approval(reason: str = "") -> None:
        try:
            from servers.voice_guard import clear_plan_approval
            clear_plan_approval(reason)
        except Exception:
            pass

    def _unsupported_plan_hints(
        self,
        plan: MilestonePlan,
        llm_tool_declarations: Optional[list[dict]],
    ) -> list[tuple[int, str, list[str]]]:
        allowed = self._llm_tool_names(llm_tool_declarations)
        issues: list[tuple[int, str, list[str]]] = []
        for milestone in plan.milestones:
            hinted = sorted({
                str(tool).strip()
                for tool in (milestone.hint_tools or [])
                if str(tool).strip() and str(tool).strip() not in {"send_response", "await_reply"}
            })
            # A hint is satisfied when an *equivalent* tool is available, not
            # only the exact one named. The planner hinting press_key while
            # type_in_field is on offer is a wording difference, not a missing
            # capability — rejecting the whole plan for it produced "I can't
            # execute the current plan" on tasks that were entirely doable.
            # The milestone executor already widens hints this way at run time;
            # planning now uses the same table.
            missing = [
                tool for tool in hinted
                if tool not in allowed
                and not self._hint_satisfied(tool, allowed)
                and not self._goal_has_dedicated_tool(milestone.goal, tool, allowed)
            ]
            if missing:
                issues.append((milestone.id, milestone.goal, missing))
        return issues

    # Hints that describe driving an app's UI by hand.
    _UI_INPUT_HINTS = frozenset({
        "type_text", "type_in_field", "click_ui", "click_element",
        "press_key", "run_shortcut", "get_ui_tree",
    })
    # Goals a dedicated tool already covers end to end.
    _MUSIC_GOAL_RE = _re.compile(
        r"(?i)\b(?:play|playing|listen|song|track|album|playlist|music|spotify)\b"
    )

    @classmethod
    def _goal_has_dedicated_tool(cls, goal: str, tool: str, allowed: set) -> bool:
        """Does an offered tool already do this goal, making the hint moot?

        "Search for and start playing 'Starboy'" was planned as typing into
        Spotify's search box, so the plan hinted type_text — which is not on
        offer for a music request — and the whole thing was rejected with "I
        can't execute the current plan". play_media *was* on offer and does the
        entire goal in one call, with no typing involved. The hint named a
        technique; the capability was there.
        """
        if tool in cls._UI_INPUT_HINTS and "play_media" in allowed:
            return bool(cls._MUSIC_GOAL_RE.search(goal or ""))
        return False

    @staticmethod
    def _hint_satisfied(tool: str, allowed: set) -> bool:
        """Is some equivalent of `tool` available in the offered surface?"""
        try:
            from tools.selector import _MILESTONE_HINT_EQUIVALENTS
        except Exception:
            return False
        return bool(_MILESTONE_HINT_EQUIVALENTS.get(tool, set()) & set(allowed))

    def _enforce_plan_tool_contract(
        self,
        plan: MilestonePlan,
        llm_tool_declarations: Optional[list[dict]],
    ) -> MilestonePlan:
        issues = self._unsupported_plan_hints(plan, llm_tool_declarations)
        if not issues:
            return plan

        fragments = []
        for milestone_id, goal, missing in issues[:3]:
            label = goal or f"Milestone {milestone_id}"
            fragments.append(f"{label} requires {', '.join(missing)}")
        clarification = (
            "I can’t execute the current plan with the tools available for this request. "
            f"{'; '.join(fragments)}. Please provide the missing source or rephrase the task."
        )
        return MilestonePlan(
            task_summary=plan.task_summary,
            needs_clarification=True,
            clarification_prompt=clarification,
            final_response=plan.final_response,
            skill_context=getattr(plan, "skill_context", ""),
            skills_used=list(getattr(plan, "skills_used", [])),
            source=f"{getattr(plan, 'source', 'milestone_planner')}_tool_contract",
        )

    def _is_pending_plan_stale(self, pending: PendingPlanState, context: perception.ContextSnapshot) -> tuple[bool, str]:
        age = _time.time() - pending.created_at
        if age > PENDING_PLAN_TTL_SECONDS:
            return True, f"older than {PENDING_PLAN_TTL_SECONDS}s"

        current_fp = self._context_fingerprint(context)
        old_app = pending.context_fingerprint.get("active_app", "")
        new_app = current_fp.get("active_app", "")
        plan_tools = self._planned_tool_names(pending.plan)
        is_web_flow_plan = bool(plan_tools.intersection(WEB_FLOW_TOOLS))
        if old_app and new_app and old_app != new_app:
            # Answering the approval prompt IS an interaction with ClarityOS, so
            # ClarityOS becoming the foreground app is not a change in the user's
            # context — it is the user replying to the question we just asked.
            # This exemption used to apply only to web-flow plans, so approving a
            # file-creation plan invalidated it, forced a replan, and (when the
            # replan failed) dead-ended on "I couldn't refresh the plan".
            if new_app in {"electron", "clarityos"}:
                pass
            elif is_web_flow_plan and old_app in BROWSER_APP_NAMES and new_app in {"electron", "clarityos"}:
                pass
            elif is_web_flow_plan and old_app in BROWSER_APP_NAMES and new_app in BROWSER_APP_NAMES:
                pass
            else:
                return True, "active app changed"

        old_domain = pending.context_fingerprint.get("browser_domain", "")
        new_domain = current_fp.get("browser_domain", "")
        if old_domain and new_domain and old_domain != new_domain:
            return True, "browser domain changed"

        return False, ""

    def _planned_tool_names(self, plan: MilestonePlan) -> set[str]:
        planned_tools: set[str] = set()
        for milestone in plan.milestones:
            planned_tools.update(
                str(tool).strip()
                for tool in milestone.hint_tools
                if str(tool).strip()
            )
        return planned_tools

    def _plan_unit_count(self, plan: MilestonePlan) -> int:
        return len(plan.milestones)

    def _should_gate_plan(self, plan: MilestonePlan) -> bool:
        communication_tools = {"send_response", "await_reply"}
        read_only_tools = {
            "read_file",
            "read_screen",
            "get_ui_tree",
            "browser_snapshot",
            "browser_read_page",
            "browser_find",
            "browser_describe_ref",
            "browser_list_tabs",
            "gdocs_read",
            "gsheets_read",
            "gdrive_search",
            "gmail_read",
            "gcal_list_events",
            "gworkspace_analyze",
            "list_directory",
            "get_web_information",
            "fetch_web_content",
            "web_scrape",
            "web_search",
            "read_page_content",
            "extract_structured_data",
            "get_page_summary",
        }
        high_risk_tools = {
            "write_file",
            "replace_in_file",
            "run_shell",
            "browser_click_ref",
            "browser_type_ref",
            "browser_select_ref",
            "browser_click_match",
            "browser_click",
            "browser_fill",
            "click_ui",
            "type_in_field",
            "click_element",
            "type_text",
            "press_key",
            "mouse_action",
            "gdocs_create",
            "gdocs_append",
            "gdocs_insert_image",
            "gsheets_create",
            "gsheets_write",
            "gsheets_append_rows",
            "gsheets_formula",
            "gslides_create",
            "gslides_add_slide",
            "gdrive_upload",
            "gmail_send",
            "gmail_draft",
            "gcal_create_event",
        }
        medium_risk_tools = {
            "open_url",
            "open_app",
            "quit_app",
            "close_window",
            "window_manager",
            "save_image",
            "clipboard_ops",
        }
        side_effect_tools = high_risk_tools | medium_risk_tools

        planned_tools = self._planned_tool_names(plan)
        if not planned_tools:
            return False

        if all(tool in read_only_tools for tool in planned_tools):
            return False

        if self._plan_unit_count(plan) == 1 and planned_tools.issubset(medium_risk_tools):
            return False

        if any(tool in high_risk_tools for tool in planned_tools):
            return True

        has_side_effect = any(tool in side_effect_tools for tool in planned_tools)
        if self._plan_unit_count(plan) >= 3 and has_side_effect:
            return True
        return False

    def _modal_steps_from_plan(self, plan: MilestonePlan) -> list[dict]:
        """Build compressed plan phases for the UI modal.
        
        Groups related steps into logical phases so the user sees a clean
        high-level summary.  The agent treats the full plan as a reference —
        actual execution adapts based on what it observes.
        """
        # success_signal is written in the past tense ("A draft reply has been
        # created in Gmail"), because it describes the state the milestone is
        # finished in. Shown as-is on an approval card — before anything has
        # run — it reads as a claim that the work is already done. Label it as
        # the completion condition instead, so planned and done stay distinct.
        steps: list[dict] = []
        for milestone in plan.milestones:
            signal = (milestone.success_signal or "").strip()
            if signal:
                detail = f"Done when: {signal[0].lower() + signal[1:]}"
            else:
                detail = ", ".join(milestone.hint_tools) or "Complete this goal"
            steps.append({
                "label": milestone.goal or f"Milestone {milestone.id}",
                "detail": detail,
                "state": "planned",
            })
        return steps

    async def _show_plan_gate(
        self,
        pending: PendingPlanState,
        context: perception.ContextSnapshot,
        provider: LLMProvider,
        ws_callback: Optional[WSCallback],
        note: str = "",
    ) -> tuple[str, bool]:
        # Showing a plan means nothing has been approved yet. Any consent left
        # over from an earlier plan must not carry into this one.
        self._clear_plan_approval("a new plan is awaiting approval")
        steps_payload = self._modal_steps_from_plan(pending.plan)
        message = "Review this plan and choose Proceed to execute it."
        if note:
            message = f"{note}\n\n{message}"

        await_payload = {
            "message": message,
            "modal": "plan",
            "steps": steps_payload,
            "plan_id": pending.plan_id,
        }
        raw = await tool_registry.execute("await_reply", await_payload)

        await_msg = message
        await_modal_data = None
        if isinstance(raw, str) and raw.startswith("AWAIT:"):
            parsed = raw[len("AWAIT:"):]
            try:
                await_modal_data = json.loads(parsed)
                await_msg = await_modal_data.get("message", await_msg)
            except (json.JSONDecodeError, TypeError):
                await_msg = parsed or await_msg

        self._pending_reply_provider = provider
        self.conversation.add_model(await_msg)

        if ws_callback:
            payload = {
                "type": "response",
                "payload": {
                    "text": await_msg,
                    "display": "card",
                    "await_input": True,
                    "app": context.active_app.lower() if context.active_app else "",
                },
            }
            if await_modal_data:
                payload["payload"]["modal_data"] = await_modal_data
            await ws_callback(payload)

        return (await_msg, True)

    async def run(
        self,
        user_text: str,
        context: perception.ContextSnapshot,
        ws_callback: Optional[WSCallback] = None,
    ) -> tuple:
        """
        Main entry point for V2 agent.
        
        Args:
            user_text: User's request text
            context: Current desktop context from perception layer
            ws_callback: WebSocket callback for streaming updates
            
        Returns:
            Tuple of (response_text, awaiting_reply)
        """
        t_start = _time.time()
        print(f"\n[AgentV2] ═══ New Request ═══")
        print(f"[AgentV2] Text: '{user_text}'")
        print(f"[AgentV2] Context: app={context.active_app}, title={context.window_title}")

        # Record user turn in conversation memory (for multi-turn context)
        self.conversation.add_user(user_text)

        # Extract facts from user message into persistent profile
        extracted = self.user_profile.extract_facts(user_text)
        if extracted:
            print(f"[AgentV2] 📝 Extracted facts: {extracted}")

        # ══════════════════════════════════════════════════════════════
        # FAST PATH: Small-talk / conversational (skip full pipeline)
        # ══════════════════════════════════════════════════════════════
        if not self._pending_plan and not self._pending_execution:
            chat_result = await self._try_conversational_fast_path(user_text, context, ws_callback)
            if chat_result is not None:
                elapsed = _time.time() - t_start
                print(f"[AgentV2] ⚡ Fast path: {elapsed:.2f}s")
                return chat_result

            # ══════════════════════════════════════════════════════════
            # FAST PATH: deterministic app launch (no LLM involved)
            # ══════════════════════════════════════════════════════════
            direct_result = await self._try_direct_windows_action(user_text, ws_callback)
            if direct_result is not None:
                elapsed = _time.time() - t_start
                print(f"[AgentV2] ⚡ Direct action: {elapsed:.2f}s")
                return direct_result

            # ══════════════════════════════════════════════════════════
            # Optional: LangGraph orchestration (CLARITYOS_USE_LANGGRAPH=1)
            # ══════════════════════════════════════════════════════════
            # Opt-in and deliberately narrow: only fresh requests, never a
            # follow-up to a pending plan or a suspended execution, because
            # those carry state the graph does not model. Everything else
            # falls through to the original path untouched.
            graph_result = await self._try_langgraph(user_text, context, ws_callback)
            if graph_result is not None:
                print(f"[AgentV2] ⏱ TOTAL (graph): {_time.time() - t_start:.2f}s")
                return graph_result

        # Stream "thinking" state to UI
        if ws_callback:
            await ws_callback({"type": "thinking"})

        # ══════════════════════════════════════════════════════════════
        # PHASE 1: SENSE — Build structured WorldState
        # ══════════════════════════════════════════════════════════════
        world_state = await self._build_world_state(user_text, context)
        gateway_context = self._current_tool_gateway_context()
        set_tool_gateway_context(
            active_app=world_state.active_app,
            browser_url=world_state.browser_url or "",
            background_mode=False,
            browser_bridge_connected=gateway_context["browser_bridge_connected"],
            browser_has_snapshot=gateway_context["browser_has_snapshot"],
            browser_session_id=gateway_context["browser_session_id"],
        )
        # Wire live progress updates from web gateway → Electron overlay
        set_web_progress_callback(ws_callback)
        print(f"[AgentV2] Intent: {world_state.intent.action.value if world_state.intent else 'unknown'}")

        planning_request = user_text

        if self._pending_execution:
            pending_execution = self._pending_execution
            age = _time.time() - pending_execution.created_at
            if age > PENDING_PLAN_TTL_SECONDS:
                print(f"[AgentV2] Pending execution expired after {age:.1f}s; discarding state.")
                self._pending_execution = None
            else:
                followup = self._classify_pending_followup(user_text)
                print(f"[AgentV2] Pending execution follow-up: {followup} ({pending_execution.execution_id})")

                if followup == "cancel":
                    self._pending_execution = None
                    cancel_message = "Cancelled the in-progress task."
                    try:
                        await tool_registry.execute("send_response", {"message": cancel_message})
                    except Exception:
                        pass
                    if ws_callback:
                        await ws_callback({
                            "type": "response",
                            "payload": {
                                "text": cancel_message,
                                "display": "card",
                                "app": context.active_app.lower() if context.active_app else "",
                            }
                        })
                    self.conversation.add_model(cancel_message)
                    return (cancel_message, False)

                provider = pending_execution.provider
                if provider is None:
                    try:
                        decision = await self.router.route(
                            pending_execution.original_user_request,
                            context_summary=f"App: {context.active_app}, Window: {context.window_title}",
                            has_screenshot=context.screenshot_path is not None,
                        )
                        provider = decision.provider
                    except RuntimeError as e:
                        error_msg = str(e)
                        if ws_callback:
                            await ws_callback({
                                "type": "response",
                                "payload": {"text": error_msg}
                            })
                        return (error_msg, False)

                resume_inputs = list(pending_execution.followup_inputs or [])
                if user_text:
                    resume_inputs.append(user_text)
                resume_request = pending_execution.original_user_request
                if resume_inputs:
                    resume_request = (
                        f"{pending_execution.original_user_request}\n\n"
                        "User follow-up:\n"
                        + "\n".join(resume_inputs)
                    )

                # Preserve the original intent from the pending execution so the
                # tool selector doesn't re-classify the follow-up text as a
                # different intent (e.g. 'analyze' instead of 'communicate').
                if pending_execution.plan and pending_execution.plan.milestones:
                    original_intent = self.intent_parser.parse(
                        pending_execution.original_user_request
                    )
                    if original_intent and original_intent.action != IntentAction.UNKNOWN:
                        world_state.intent = original_intent

                selected_tools, llm_tool_declarations = self._select_tool_surface(
                    resume_request,
                    context,
                    world_state,
                )
                pending_execution.plan = self._enforce_plan_tool_contract(
                    pending_execution.plan,
                    llm_tool_declarations,
                )
                if pending_execution.plan.needs_clarification:
                    self._pending_execution = None
                    result = await self._handle_clarification(
                        pending_execution.plan.clarification_prompt,
                        provider,
                        context,
                        ws_callback,
                    )
                    return result

                pending_execution.provider = provider
                pending_execution.selected_tools = list(selected_tools)
                pending_execution.followup_inputs = resume_inputs
                self._pending_execution = None

                final_response, awaiting = await self._execute_milestone_plan(
                    plan=pending_execution.plan,
                    world_state=world_state,
                    provider=provider,
                    context=context,
                    user_text=resume_request,
                    llm_tool_declarations=llm_tool_declarations,
                    ws_callback=ws_callback,
                    pending_execution=pending_execution,
                    followup_inputs=resume_inputs,
                )
                print(f"[AgentV2] ⏱ TOTAL: {_time.time() - t_start:.2f}s")
                return (final_response, awaiting)

        # Pending-plan follow-up handling (approve/cancel/modify)
        if self._pending_plan:
            pending = self._pending_plan
            followup = self._classify_pending_followup(user_text)
            print(f"[AgentV2] Pending plan follow-up: {followup} ({pending.plan_id})")

            if followup == "show":
                # Re-send the SAME pending plan: same plan_id, no regeneration,
                # no second approval to reason about. Asking to see the plan is
                # not consent to run it, so this still returns awaiting=True.
                print(f"[AgentV2] Re-displaying pending plan {pending.plan_id} on request")
                return await self._show_plan_gate(
                    pending=pending,
                    context=context,
                    provider=pending.provider or provider,
                    ws_callback=ws_callback,
                    note="Here's the plan again — choose Proceed to run it, or Cancel.",
                )

            if followup == "cancel":
                self._pending_plan = None
                cancel_message = "Plan cancelled."
                try:
                    await tool_registry.execute("send_response", {"message": cancel_message})
                except Exception:
                    pass
                if ws_callback:
                    await ws_callback({
                        "type": "response",
                        "payload": {
                            "text": cancel_message,
                            "display": "card",
                            "app": context.active_app.lower() if context.active_app else "",
                        }
                    })
                self.conversation.add_model(cancel_message)
                return (cancel_message, False)

            if followup == "approve":
                stale, stale_reason = self._is_pending_plan_stale(pending, context)
                if stale and pending.stale_replan_count < 1 and self.use_planning:
                    print(f"[AgentV2] Pending plan stale ({stale_reason}); regenerating once.")
                    self._pending_plan = None

                    provider = pending.provider
                    if provider is None:
                        decision = await self.router.route(
                            pending.original_user_request,
                            context_summary=f"App: {context.active_app}, Window: {context.window_title}",
                            has_screenshot=context.screenshot_path is not None,
                        )
                        provider = decision.provider

                    if self.planner is None:
                        self.planner = TaskPlanner(provider=provider, tool_registry=tool_registry)
                    else:
                        self.planner.provider = provider

                    selected_tools, llm_tool_declarations = self._select_tool_surface(
                        pending.original_user_request,
                        context,
                        world_state,
                    )
                    if ws_callback:
                        await ws_callback({"type": "doing", "text": "Refreshing plan due to context change...", "tool": "planner"})

                    refreshed_plan = await self.planner.create_milestone_plan(
                        user_request=pending.original_user_request,
                        world_state=world_state,
                        conversation_history=self.conversation.get_history(),
                        available_tools=selected_tools,
                    )
                    if refreshed_plan is None:
                        # The user already approved a concrete plan. If the
                        # refresh cannot run (provider down, quota spent), fall
                        # back to the plan they approved rather than discarding
                        # their consent and asking them to start over.
                        print("[AgentV2] Replan unavailable — running the approved plan as-is.")
                        if ws_callback:
                            await ws_callback({
                                "type": "doing",
                                "text": "Couldn't refresh the plan — running the one you approved.",
                                "tool": "planner",
                            })
                        refreshed_plan = pending.plan
                    refreshed_plan = self._enforce_plan_tool_contract(refreshed_plan, llm_tool_declarations)
                    if refreshed_plan.needs_clarification:
                        result = await self._handle_clarification(
                            refreshed_plan.clarification_prompt,
                            provider,
                            context,
                            ws_callback,
                        )
                        return result

                    refreshed_pending = PendingPlanState(
                        plan_id=uuid.uuid4().hex[:10],
                        plan=refreshed_plan,
                        created_at=_time.time(),
                        context_fingerprint=self._context_fingerprint(context),
                        provider=provider,
                        original_user_request=pending.original_user_request,
                        selected_tools=list(selected_tools),
                        stale_replan_count=pending.stale_replan_count + 1,
                    )
                    self._pending_plan = refreshed_pending
                    result = await self._show_plan_gate(
                        pending=refreshed_pending,
                        context=context,
                        provider=provider,
                        ws_callback=ws_callback,
                        note="Context changed since the original plan, so I refreshed it.",
                    )
                    return result

                if stale:
                    print(f"[AgentV2] Pending plan stale ({stale_reason}) but refresh already used; executing frozen plan.")

                provider = pending.provider
                if provider is None:
                    try:
                        decision = await self.router.route(
                            pending.original_user_request,
                            context_summary=f"App: {context.active_app}, Window: {context.window_title}",
                            has_screenshot=context.screenshot_path is not None,
                        )
                        provider = decision.provider
                    except RuntimeError as e:
                        error_msg = str(e)
                        if ws_callback:
                            await ws_callback({
                                "type": "response",
                                "payload": {"text": error_msg}
                            })
                        return (error_msg, False)

                self._pending_plan = None
                print(f"[AgentV2] Executing frozen approved plan: {pending.plan.task_summary}")
                # The plan card described these actions and the user pressed
                # Proceed. Without this the per-tool guard asked a second time
                # for the same action — and an unanswered second prompt came
                # back as "The user did not approve this action" on a plan they
                # had just approved.
                self._note_plan_approval(pending.plan)
                llm_tool_declarations = self.tool_selector.get_llm_tool_declarations(pending.selected_tools)
                final_response, awaiting = await self._execute_milestone_plan(
                    plan=pending.plan,
                    world_state=world_state,
                    provider=provider,
                    context=context,
                    user_text=pending.original_user_request,
                    llm_tool_declarations=llm_tool_declarations,
                    ws_callback=ws_callback,
                )
                print(f"[AgentV2] ⏱ TOTAL: {_time.time() - t_start:.2f}s")
                return (final_response, awaiting)

            # Modification path: clear frozen plan and replan with explicit plan context.
            previous_plan = pending.plan.to_prompt_string()
            previous_request = pending.original_user_request
            self._pending_plan = None
            planning_request = (
                f"{user_text}\n\n"
                "Previous proposed plan (modify this):\n"
                f"{previous_plan}\n\n"
                "Original request:\n"
                f"{previous_request}"
            )
            print("[AgentV2] Replanning from user modifications to pending plan.")

        # ══════════════════════════════════════════════════════════════
        # ROUTING — Select model tier
        # ══════════════════════════════════════════════════════════════
        if self._pending_reply_provider:
            provider = self._pending_reply_provider
            self._pending_reply_provider = None
            print(f"[AgentV2] Resuming with {provider.name}")
        else:
            # ── Instant acknowledgment ──
            if ws_callback:
                ack = self._pick_ack(user_text)
                await ws_callback({"type": "ack", "text": ack})

            # Immediate feedback while routing LLM runs
            if ws_callback:
                await ws_callback({"type": "doing", "text": "Analyzing…", "tool": "router"})
            try:
                decision = await self.router.route(
                    planning_request,
                    context_summary=f"App: {context.active_app}, Window: {context.window_title}",
                    has_screenshot=context.screenshot_path is not None,
                )
                provider = decision.provider
                print(f"[AgentV2] Using {provider.name} ({decision.reason})")
            except RuntimeError as e:
                error_msg = str(e)
                if ws_callback:
                    await ws_callback({
                        "type": "response",
                        "payload": {"text": error_msg}
                    })
                return (error_msg, False)

        # Initialize planner with provider
        if self.planner is None:
            self.planner = TaskPlanner(provider=provider, tool_registry=tool_registry)
        else:
            self.planner.provider = provider

        world_state.task_graph = self.planner.intent_parser.extract_task_graph(
            planning_request,
            world_state,
        )

        # ══════════════════════════════════════════════════════════════
        # PHASE 2: PLAN — Generate execution plan (parallelised)
        # ══════════════════════════════════════════════════════════════
        # Tool selection is CPU-only (deterministic, ~0ms).  We run it
        # first so its result feeds into the LLM planning call.
        selected_tools, llm_tool_declarations = self._select_tool_surface(
            planning_request,
            context,
            world_state,
        )

        # Stream progress immediately — don't wait for the LLM
        if ws_callback:
            await ws_callback({"type": "doing", "text": "Planning…", "tool": "planner"})

        milestone_plan = await self.planner.create_milestone_plan(
            user_request=planning_request,
            world_state=world_state,
            conversation_history=self.conversation.get_history(),
            available_tools=selected_tools,
        )
        if milestone_plan is None:
            milestone_plan = MilestonePlan(
                task_summary=planning_request,
                needs_clarification=True,
                clarification_prompt=self.planner.planning_failure_message(),
            )
        milestone_plan = self._enforce_plan_tool_contract(milestone_plan, llm_tool_declarations)

        print(f"[AgentV2] Milestone plan: {milestone_plan.task_summary} "
              f"({len(milestone_plan.milestones)} milestones)")

        # Handle clarification needed
        if milestone_plan.needs_clarification:
            result = await self._handle_clarification(
                milestone_plan.clarification_prompt,
                provider, 
                context, 
                ws_callback
            )
            return result

        if self._should_gate_plan(milestone_plan):
            pending = PendingPlanState(
                plan_id=uuid.uuid4().hex[:10],
                plan=milestone_plan,
                created_at=_time.time(),
                context_fingerprint=self._context_fingerprint(context),
                provider=provider,
                original_user_request=planning_request,
                selected_tools=list(selected_tools),
            )
            self._pending_plan = pending
            print(f"[AgentV2] Plan gated for approval: {pending.plan_id}")
            result = await self._show_plan_gate(
                pending=pending,
                context=context,
                provider=provider,
                ws_callback=ws_callback,
            )
            print(f"[AgentV2] ⏱ TOTAL: {_time.time() - t_start:.2f}s")
            return result

        # ══════════════════════════════════════════════════════════════
        # PHASE 3 & 4: ACT + VERIFY — Execute milestone plan
        # ══════════════════════════════════════════════════════════════
        if ws_callback:
            first_goal = milestone_plan.milestones[0].goal if milestone_plan.milestones else "Working"
            await ws_callback({"type": "doing", "text": first_goal, "tool": "executor"})

        final_response, awaiting = await self._execute_milestone_plan(
            plan=milestone_plan,
            world_state=world_state,
            provider=provider,
            context=context,
            user_text=planning_request,
            llm_tool_declarations=llm_tool_declarations,
            ws_callback=ws_callback
        )

        print(f"[AgentV2] ⏱ TOTAL: {_time.time() - t_start:.2f}s")

        # ── Background memory curator: decide what to persist ──
        asyncio.ensure_future(self._curate_vault(user_text, final_response))

        return (final_response, awaiting)

    async def _curate_vault(self, user_text: str, agent_response: str) -> None:
        """Background curator: review the conversation and decide what to persist.

        Spawned as a fire-and-forget task after ``run()`` completes.
        Uses a lightweight Flash call to judge whether any facts, contacts,
        preferences, or research snippets from the exchange are worth storing
        permanently in the vault.
        """
        try:
            # Only curate if there's meaningful content
            if not user_text or len(user_text.strip()) < 10:
                return
            if not agent_response or len(agent_response.strip()) < 20:
                return

            # Build a compact context for the curator prompt
            recent_actions = self.working_memory.get_recent_actions(5)
            action_summary = "; ".join(
                f"{a.tool}→{str(a.result_summary or '')[:60]}"
                for a in recent_actions
            ) if recent_actions else "none"

            curator_prompt = (
                "You are a memory curator. Review this conversation exchange and decide "
                "if anything should be permanently stored in the user's vault memory.\n\n"
                f"User said: {user_text[:500]}\n"
                f"Agent response: {agent_response[:500]}\n"
                f"Recent actions: {action_summary[:300]}\n\n"
                "If there's something worth remembering (a contact detail, preference, "
                "research finding, important fact, file reference, etc.), respond with "
                "a JSON object: {\"store\": true, \"category\": \"...\", \"title\": \"...\", "
                "\"content\": \"...\", \"tags\": [\"...\"]}\n"
                "If nothing is worth storing, respond with: {\"store\": false}\n"
                "Be selective — only store genuinely useful long-term information."
            )

            # Use Flash for speed (lightweight model)
            provider = self.router.get("flash")
            if not provider:
                return

            result = await provider.chat(
                system_prompt="You are a concise memory curator. Respond ONLY with valid JSON.",
                messages=[{"role": "user", "parts": [{"text": curator_prompt}]}],
                temperature=0.1,
            )

            response_text = (result.get("text", "") or "").strip()
            if not response_text:
                return

            # Parse the curator's decision
            # Strip markdown code fences if present
            if response_text.startswith("```"):
                response_text = response_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            decision = json.loads(response_text)
            if not decision.get("store"):
                return

            # Store in vault
            category = decision.get("category", "notes")
            title = decision.get("title", "")
            content = decision.get("content", "")
            tags = decision.get("tags", [])

            if title and content:
                self.vault.store(
                    category=category,
                    title=title,
                    content=content,
                    tags=tags if isinstance(tags, list) else [],
                    source=f"auto-curated from conversation",
                )
                print(f"[AgentV2] 🧠 Curator stored: [{category}] {title[:60]}")

        except json.JSONDecodeError:
            pass  # Curator response wasn't valid JSON — skip silently
        except Exception as e:
            print(f"[AgentV2] ⚠ Curator error (non-fatal): {e}")

    async def _build_world_state(
        self,
        user_text: str,
        context: perception.ContextSnapshot
    ) -> WorldState:
        """Build structured WorldState from context and user text."""
        
        # Extract entities from user text
        entities = self.entity_extractor.extract(user_text)
        
        # Parse intent
        intent = self.intent_parser.parse(user_text)
        
        # Build world state
        world_state = WorldState(
            # Desktop state
            active_app=context.active_app or "",
            window_title=context.window_title or "",
            browser_url=context.browser_url,
            
            # Extracted entities
            mentioned_apps=entities.get("apps", []),
            mentioned_files=entities.get("files", []),
            mentioned_urls=entities.get("urls", []),
            
            # Clipboard
            clipboard_content=context.clipboard,
            
            # Selected text
            selected_text=getattr(context, 'selected_text', None),
            
            # Screenshot
            has_screenshot=context.screenshot_path is not None,
            screenshot_path=context.screenshot_path,
            
            # Intent
            intent=intent,
            
            # Metadata
            timestamp=context.timestamp
        )
        
        return world_state

    async def _handle_clarification(
        self,
        prompt: str,
        provider: LLMProvider,
        context: perception.ContextSnapshot,
        ws_callback: Optional[WSCallback]
    ) -> tuple:
        """Handle a request that needs clarification."""
        print(f"[AgentV2] ⏳ Awaiting clarification: {prompt}")
        
        # Add to conversation memory
        self.conversation.add_model(prompt)
        
        # Lock provider for follow-up
        self._pending_reply_provider = provider
        
        if ws_callback:
            await ws_callback({
                "type": "response",
                "payload": {
                    "text": prompt,
                    "display": "card",
                    "await_input": True,
                    "app": context.active_app.lower() if context.active_app else ""
                }
            })
        
        return (prompt, True)

    # ═══════════════════════════════════════════════════════════════
    #  Adaptive Plan Execution (LLM-in-the-loop)
    # ═══════════════════════════════════════════════════════════════

    async def _perceive_environment(self, followup_inputs: Optional[list[str]] = None) -> dict:
        """Quick perception of the current desktop/browser state for step reasoning."""
        env: dict = {}
        try:
            active_app, window_title, clipboard = await asyncio.gather(
                perception.get_active_app(),
                perception.get_window_title(),
                perception.get_clipboard(),
            )
            env["active_app"] = active_app
            env["window_title"] = window_title
            if clipboard:
                env["clipboard"] = clipboard[:500]
            if active_app.lower() in perception.BROWSERS:
                browser_url, selected_text = await asyncio.gather(
                    perception.get_browser_url(active_app),
                    perception.get_browser_selected_text(active_app),
                )
                env["browser_url"] = browser_url or ""
                if selected_text:
                    env["selected_text"] = selected_text[:500]
        except Exception as e:
            print(f"[AgentV2] ⚠ Perception failed: {e}")
        if followup_inputs:
            env["user_followups"] = " || ".join(text[:200] for text in followup_inputs if text)
        last_typed_text = self.working_memory.get_last_typed_text()
        if last_typed_text:
            env["last_typed_text"] = last_typed_text[:500]
        search_leads = self.working_memory.get_search_leads()
        if search_leads:
            env["search_lead_count"] = len(search_leads)
            top_leads = []
            for lead in search_leads[-3:]:
                title = str(lead.get("title", "")).strip()
                domain = str(lead.get("domain", "")).strip()
                status = "opened" if lead.get("opened") else "unopened"
                row = " | ".join(part for part in (title, domain, status) if part)
                if row:
                    top_leads.append(row[:180])
            if top_leads:
                env["search_leads"] = " || ".join(top_leads)
        return env

    # ═══════════════════════════════════════════════════════════════
    #  Milestone-Based Execution (LLM micro-loop per milestone)
    # ═══════════════════════════════════════════════════════════════

    async def _execute_milestone_plan(
        self,
        plan: MilestonePlan,
        world_state: WorldState,
        provider: LLMProvider,
        context: perception.ContextSnapshot,
        user_text: str,
        llm_tool_declarations: Optional[list[dict]],
        ws_callback: Optional[WSCallback],
        pending_execution: Optional[PendingExecutionState] = None,
        followup_inputs: Optional[list[str]] = None,
    ) -> tuple:
        """Execute a milestone-based plan using the LLM micro-loop.

        Each milestone is executed autonomously — the LLM decides tool calls
        within the milestone's scope until the success_signal is met or
        the runtime safety cap is reached.

        Returns:
            Tuple of (final_response, awaiting_reply)
        """
        # Verdicts belong to one request; a stale note would describe the
        # previous task's retrieval, and a stale success would let this task
        # announce "Summarizing…" on the strength of the last one's content.
        self._partial_retrieval_notes = []
        self._partial_note_emitted = False
        # One authoritative reading of what the user is looking at, taken before
        # any tool runs. Everything downstream binds to this rather than
        # re-deriving context per step, which is how a request about Gmail
        # ended up answered from a window the agent had just opened itself.
        try:
            from agent.current_context import capture as _capture_ctx, binding_problem
            self._current_context = _capture_ctx(task_id=str(id(plan)))

            # A stale snapshot is not a dead end — the extension can be asked
            # for a fresh one. Previously this reported "the extension has not
            # published a page yet" and stopped, so the user was told to reload
            # a page that was already open and fine. The MV3 service worker
            # sleeps after ~30s idle and drops the tab-switch push, which is
            # exactly when this happens.
            ctx = self._current_context
            if ctx and ctx.is_browser and not ctx.snapshot_fresh:
                print("[AgentV2] |  browser snapshot is stale — asking the extension to re-read")
                try:
                    await tool_registry.execute("browser_refresh_refs", {})
                    self._current_context = _capture_ctx(task_id=str(id(plan)))
                    if self._current_context.snapshot_fresh:
                        print("[AgentV2] |  snapshot refreshed from the extension")
                except Exception as _ref_exc:
                    print(f"[AgentV2] |  refresh failed ({_ref_exc.__class__.__name__})")

            self._binding_problem = binding_problem(self._current_context, user_text)
            print(f"[AgentV2] |  current context: {self._current_context.describe()}")
            if self._binding_problem:
                # Not fatal on its own — a compatible retrieval may still work.
                # Kept so that if the task does fail, the user is told the real
                # reason instead of a list of milestone goals.
                print(f"[AgentV2] |  context binding problem: {self._binding_problem[:110]}")
        except Exception as _ctx_exc:
            self._current_context = None
            self._binding_problem = ""
            print(f"[AgentV2] |  (context capture failed: {_ctx_exc})")

        try:
            from agent.retrieval_guard import (reset_retrieval_state, invalidate_url,
                                               note_content)
            reset_retrieval_state()
            # A URL the user typed is grounded by definition — it came from
            # them, not from the model's recollection.
            note_content(user_text)
            # "that link was wrong" must not be answered with the same link.
            # The ledger is per task, so carry the rejection across explicitly.
            if self._is_correction(user_text):
                for stale in getattr(self, "_last_answer_urls", ()) or ():
                    invalidate_url(stale)
                if getattr(self, "_last_answer_urls", None):
                    print(f"[AgentV2] |  correction: invalidated "
                          f"{len(self._last_answer_urls)} previously offered link(s)")
        except Exception:
            pass

        from agent.milestone_executor import get_milestone_executor

        t_start = _time.time()
        executor = get_milestone_executor(provider, llm_tool_declarations or tool_registry.declarations())
        request_scope_tool_names = {
            str(decl.get("name", "")).strip()
            for decl in (llm_tool_declarations or tool_registry.declarations())
            if str(decl.get("name", "")).strip()
        }
        deliverables: dict[int, str] = dict(pending_execution.deliverables) if pending_execution else {}
        milestone_step_results: dict[int, str] = (
            dict(pending_execution.milestone_step_results) if pending_execution else {}
        )
        milestone_step_result_idx = pending_execution.milestone_step_result_idx if pending_execution else 0
        blocked_await_signatures = set(pending_execution.blocked_await_signatures or []) if pending_execution else set()
        followup_inputs = list(
            followup_inputs
            or (pending_execution.followup_inputs if pending_execution else [])
            or []
        )
        last_await_payload: Optional[dict] = dict(pending_execution.await_payload) if pending_execution else None
        self._last_opened_url = pending_execution.last_opened_url if pending_execution else ""

        print(f"\n[AgentV2] ═══ Milestone Plan: {plan.task_summary} ═══")
        print(f"[AgentV2]   Milestones: {len(plan.milestones)}")
        for m in plan.milestones:
            deps = f" (depends: {m.depends_on})" if m.depends_on else ""
            print(f"[AgentV2]   [{m.id}] {m.goal} — signal: {m.success_signal}{deps}")
        print(f"[AgentV2] ═══{'═' * 50}═══\n")

        # Reset Glance perception for this new task
        from agent.glance import get_glance as _get_glance
        _get_glance().reset()

        is_research = self._looks_like_research_doc_task(user_text, plan.task_summary or "")

        # Tracks whether a send_response was already forwarded to ws_callback
        # inside the milestone executor.  Suppresses the duplicate plain-text
        # fallback response that _execute_milestone_plan would otherwise send.
        response_emitted_to_ui = False

        # Track whether the last tool was UI-mutating (drives passive visual injection)
        last_tool_was_ui_mutating = False

        async def milestone_visual_context() -> str:
            """Gather DOM snapshot + screenshot description for passive injection."""
            parts: list[str] = []
            try:
                from browser.store import browser_store
                snap = browser_store.get_snapshot()
                if snap and snap.elements:
                    dom_lines = [f"Page: {snap.title} ({snap.url})"]
                    for el in snap.elements[:30]:
                        label = el.primary_label()[:60] if el.primary_label() else el.tag
                        dom_lines.append(f"  [{el.ref_id}] {el.role or el.tag}: {label}")
                    parts.append("\n".join(dom_lines))
            except Exception as e:
                print(f"[AgentV2] ⚠ DOM snapshot for visual context failed: {e}")

            try:
                screenshot_path = await perception.capture_screenshot()
                if screenshot_path:
                    parts.append(f"Screenshot captured: {screenshot_path}")
            except Exception as e:
                print(f"[AgentV2] ⚠ Screenshot for visual context failed: {e}")

            return "\n".join(parts) if parts else ""

        async def milestone_env_perceiver() -> dict:
            env = await self._perceive_environment(followup_inputs=followup_inputs)
            # Passive visual injection: only after UI-mutating tools
            if last_tool_was_ui_mutating:
                try:
                    visual = await milestone_visual_context()
                    if visual:
                        env["visual_state"] = visual[:1500]
                except Exception as e:
                    print(f"[AgentV2] ⚠ Passive visual injection failed: {e}")
            return env

        # Build tool-executor closure that reuses the existing step infrastructure
        async def tool_executor(tool: str, args: dict) -> tuple[str, bool]:
            nonlocal milestone_step_result_idx
            nonlocal last_await_payload
            nonlocal last_tool_was_ui_mutating
            nonlocal response_emitted_to_ui

            # Research synthesis for writing tools + send_response
            _WRITING_FIELDS = {
                "gdocs_create": "body",
                "gdocs_append": "text",
                "write_file": "content",
                "send_response": "message",   # inject research into the reply
            }
            target_field = _WRITING_FIELDS.get(tool)
            if target_field:
                # For send_response: synthesise whenever working_memory has snippets
                # (not gated on is_research so verbal research replies also get content)
                wm_snippets_available = bool(self.working_memory.get_research_snippets())
                should_synthesize = (
                    is_research
                    if tool != "send_response"
                    else wm_snippets_available
                )
                # Once the provider has terminally failed (quota/auth), every
                # further model call fails the same way and spends what little
                # headroom remains. Synthesis is optional; delivering the
                # failure message is not.
                if should_synthesize:
                    try:
                        from agent import milestone_executor as _mx_chk
                        if getattr(_mx_chk._instance, "_fatal_provider_error", ""):
                            should_synthesize = False
                            print("[AgentV2] |  skipping synthesis: provider terminally failed")
                    except Exception:
                        pass
                if should_synthesize:
                    snippets = self._collect_research_snippets(milestone_step_results)
                    if snippets:
                        try:
                            synthesized = await self._synthesize_research_body(
                                provider=provider,
                                user_text=user_text,
                                task_summary=plan.task_summary or "",
                                snippets=snippets,
                                for_response=(tool == "send_response"),
                            )
                            if synthesized:
                                args[target_field] = synthesized
                                if tool == "send_response":
                                    # Upgrade to rich modal so markdown renders properly
                                    args.setdefault("modal", "rich")
                                    args.setdefault("title", "Research Results")
                                print(f"[AgentV2] 📝 Synthesized research for {tool} ({len(synthesized)} chars)")
                        except Exception as e:
                            print(f"[AgentV2] ⚠ Research synthesis failed: {e}")

            temp_step = ExecutionStep(
                id=0, tool=tool, args=dict(args),
                description=f"{tool}",
            )
            success = await self._execute_step(
                temp_step, world_state, ws_callback,
                user_text=user_text,
                task_summary=plan.task_summary or "",
            )
            result = str(temp_step.result or "") if success else str(temp_step.error or "")

            if success and tool == "await_reply":
                last_await_payload = dict(temp_step.modal_data or {})
                if "message" not in last_await_payload:
                    last_await_payload["message"] = result
                if ws_callback:
                    payload = {
                        "type": "response",
                        "payload": {
                            "text": result,
                            "display": "card",
                            "await_input": True,
                            "app": context.active_app.lower() if context.active_app else "",
                        },
                    }
                    if temp_step.modal_data:
                        payload["payload"]["modal_data"] = temp_step.modal_data
                    await ws_callback(payload)

            if success and tool == "send_response":
                # Forward the rich modal payload immediately so the UI renders
                # the correct modal type (rich, list, cards, etc.) rather than
                # waiting for the outer plain-text fallback send.
                modal_data = temp_step.modal_data or {}
                if ws_callback:
                    forward_payload: dict = {
                        **modal_data,
                        "display": "card",
                        "app": context.active_app.lower() if context.active_app else "",
                    }
                    await ws_callback({"type": "response", "payload": forward_payload})
                # Record so the outer _execute_milestone_plan fallback is skipped
                response_emitted_to_ui = True
                plan.final_response = modal_data.get("message", result) or result

            if success and tool != "await_reply":
                milestone_step_result_idx += 1
                milestone_step_results[milestone_step_result_idx] = result

            # Track UI-mutating tools for passive visual injection
            from agent.constants import UI_MUTATING_TOOLS as _UI_MUTATING_TOOLS
            last_tool_was_ui_mutating = tool in _UI_MUTATING_TOOLS

            return (result, success)

        # ── Bridge: wraps executor.execute_milestone() for SubAgentManager ──
        # SubAgentManager calls this per milestone; it must capture all closure
        # state (provider, ws_callback, etc.) and return (success, result_summary).

        class _AwaitReplySignal(Exception):
            """Sentinel raised when a milestone suspends via await_reply."""
            def __init__(self, suspended_milestone_id: int, await_payload: dict,
                         await_data: dict):
                self.suspended_milestone_id = suspended_milestone_id
                self.await_payload = await_payload
                self.await_data = await_data

        async def milestone_execute_fn(
            milestone,
            current_deliverables: dict,
        ) -> tuple:
            nonlocal milestone_step_result_idx, last_await_payload

            plan.mark_milestone_in_progress(milestone.id)

            if ws_callback:
                total_m = len(plan.milestones)
                progress_idx = next(
                    (i + 1 for i, m in enumerate(plan.milestones) if m.id == milestone.id),
                    milestone.id,
                )
                # Milestone goals are written by the planner before anything
                # runs, so a goal like "Summarize the code" was announced even
                # when the editor read had already been refused. Only announce
                # what the current state supports.
                from agent.retrieval_guard import (
                    progress_text as _progress_text,
                    last_failure_reason as _last_failure,
                )
                _goal_text = _progress_text(milestone.goal, tool="milestone")
                if _goal_text is None:
                    await ws_callback({
                        "type": "doing",
                        "text": f"Couldn't get the content needed for this step. "
                                f"{_last_failure()}".strip(),
                        "tool": "milestone",
                        "variant": "failed",
                    })
                else:
                    await ws_callback({
                        "type": "doing",
                        "text": f"Milestone {progress_idx}/{total_m}: {_goal_text}",
                        "tool": "milestone",
                    })

            success, result_summary = await executor.execute_milestone(
                milestone=milestone,
                plan=plan,
                env_perceiver=milestone_env_perceiver,
                tool_executor=tool_executor,
                deliverables=current_deliverables,
                skill_context=getattr(plan, "skill_context", ""),
                request_scope_tool_names=request_scope_tool_names,
                blocked_await_signatures=blocked_await_signatures,
                ws_callback=ws_callback,
            )

            # Detect await_reply suspension — raise a sentinel so the outer
            # method can store PendingExecutionState and return to the caller.
            if not success and result_summary.startswith("AWAIT_REPLY:"):
                try:
                    await_data = json.loads(result_summary[len("AWAIT_REPLY:"):])
                except (json.JSONDecodeError, TypeError):
                    await_data = {}
                if await_data.get("signature"):
                    blocked_await_signatures.add(str(await_data["signature"]))
                await_payload = dict(last_await_payload or {})
                if "message" not in await_payload:
                    await_payload["message"] = str(
                        await_data.get("message") or "I need more information."
                    )
                raise _AwaitReplySignal(
                    suspended_milestone_id=milestone.id,
                    await_payload=await_payload,
                    await_data=await_data,
                )

            return success, result_summary

        # ── Execute milestones via SubAgentManager (parallel where possible) ──
        from multi_agent.sub_agent_manager import SubAgentManager

        sub_manager = SubAgentManager(
            provider=provider,
            tool_declarations=llm_tool_declarations or tool_registry.declarations(),
            system_prompt="",
            ws_callback=ws_callback,
        )

        try:
            deliverables = await sub_manager.dispatch(
                plan=plan,
                existing_deliverables=deliverables,
                execute_fn=milestone_execute_fn,
                # Re-planning is another model call. After a terminal provider
                # failure it cannot succeed, so the run must end rather than
                # re-plan its way into a second quota error.
                replan_fn=self._replan_unless_provider_dead(is_research),
            )
        except _AwaitReplySignal as sig:
            # A milestone requested a reply from the user — suspend execution.
            self._pending_execution = PendingExecutionState(
                execution_id=uuid.uuid4().hex[:10],
                plan=plan,
                created_at=_time.time(),
                provider=provider,
                original_user_request=(
                    pending_execution.original_user_request
                    if pending_execution is not None
                    else user_text
                ),
                selected_tools=[
                    str(decl.get("name", "")).strip()
                    for decl in (llm_tool_declarations or [])
                    if str(decl.get("name", "")).strip()
                ],
                deliverables=dict(deliverables),
                milestone_step_results=dict(milestone_step_results),
                milestone_step_result_idx=milestone_step_result_idx,
                suspended_milestone_id=sig.suspended_milestone_id,
                await_payload=sig.await_payload,
                followup_inputs=list(followup_inputs),
                blocked_await_signatures=sorted(blocked_await_signatures),
                last_opened_url=self._last_opened_url,
            )
            await_message = str(
                sig.await_payload.get("message") or "I need more information."
            )
            self.conversation.add_model(await_message)
            return await_message, True

        elapsed = _time.time() - t_start

        # ── Execution Summary ──
        completed = [m for m in plan.milestones if m.status == MilestoneStatus.COMPLETED]
        failed = [m for m in plan.milestones if m.status == MilestoneStatus.FAILED]
        skipped = [m for m in plan.milestones if m.status == MilestoneStatus.SKIPPED]

        print(f"\n[AgentV2] ═══ Milestone Execution Summary ═══")
        print(f"[AgentV2]   Total: {len(plan.milestones)} | "
              f"Completed: {len(completed)} | Failed: {len(failed)} | Skipped: {len(skipped)}")
        print(f"[AgentV2]   Duration: {elapsed:.2f}s")
        print(f"[AgentV2]   Progress: {plan.progress_percentage():.0f}%")
        for m in plan.milestones:
            icon = {
                "COMPLETED": "✓", "FAILED": "✗", "SKIPPED": "⏭",
                "PENDING": "◻", "IN_PROGRESS": "▶",
            }.get(m.status.name, "?")
            hint = f" → {m.result_summary[:80]}" if m.result_summary else ""
            print(f"[AgentV2]   {icon} [{m.id}] {m.goal}{hint}")
        print(f"[AgentV2] ═══{'═' * 50}═══\n")

        # ── Build final response ──
        if plan.is_complete():
            final_response = plan.final_response or "Done!"
            if deliverables and not plan.final_response:
                last_deliv = list(deliverables.values())[-1]
                if last_deliv and len(last_deliv) > 10:
                    final_response = last_deliv
        elif plan.has_failed():
            # If the run stopped because the model itself was unavailable, say
            # that. Listing the milestone goals hid a quota/auth failure behind
            # "Sorry, I couldn't complete: Extract the text content of …",
            # which tells the user nothing they can act on.
            provider_stop = ""
            try:
                from agent import milestone_executor as _mx
                provider_stop = getattr(_mx._instance, "_fatal_provider_error", "") or ""
            except Exception:
                provider_stop = ""
            if provider_stop:
                final_response = provider_stop
            elif any(str(m.result_summary or "").startswith("TASK_DEADLINE:")
                     for m in failed):
                # Listing the goals here claimed nothing about *why*. Running
                # out of time is actionable in a way "Sorry, I couldn't
                # complete: Extract the hackathon link" is not.
                reason = ""
                try:
                    from agent.retrieval_guard import last_failure_reason
                    reason = last_failure_reason()
                except Exception:
                    pass
                final_response = (
                    "I ran out of time on this one before I could get the content I "
                    "needed, so I stopped instead of retrying."
                    + (f" The last problem was: {reason}" if reason else "")
                )
            elif getattr(self, "_binding_problem", ""):
                # The request was about a window/tab we could not bind to. That
                # is the actual reason, and it tells the user what to change.
                final_response = self._binding_problem
            else:
                # Repeating the goal back says nothing the user did not
                # already know: "Sorry, I couldn't complete: Find 3 similar
                # movie recommendations" gives them no idea what went wrong or
                # what to do next. The executor records why each milestone
                # stopped — say that.
                first = failed[0] if failed else None
                goal = str(getattr(first, "goal", "") or "").strip()
                why = str(getattr(first, "result_summary", "") or "").strip()
                for noise in ("TASK_DEADLINE:", "STALLED:", "FAILED:"):
                    if why.startswith(noise):
                        why = why[len(noise):].strip()
                if why and why.lower() != goal.lower():
                    final_response = f"I couldn't finish \"{goal}\" - {why}"
                else:
                    final_response = (
                        f"I couldn't finish \"{goal}\", and I don't have a specific "
                        f"reason to give you - nothing I tried moved it forward."
                    )
                if len(failed) > 1:
                    final_response += f" ({len(failed) - 1} other step(s) also did not finish.)"
        else:
            final_response = f"Partially completed ({len(completed)}/{len(plan.milestones)} milestones)."

        # ── URL grounding ──
        # A link in the answer must have come from content that was actually
        # retrieved and validated this task. Otherwise it is the model's own
        # recollection, which is how a confident but wrong hackathon URL was
        # handed over after the screen read had failed with a 503.
        try:
            from agent.retrieval_guard import ungrounded_urls as _ungrounded
            stray = _ungrounded(final_response)
            if stray:
                print(f"[AgentV2] |  ungrounded URL(s) in answer: {len(stray)}")
                for bad in stray:
                    final_response = final_response.replace(bad, "[unverified link removed]")
                final_response = (
                    final_response.rstrip()
                    + "\n\nI couldn't verify that link against the page itself, so I've "
                      "left it out rather than risk sending you somewhere wrong."
                )
            # Remember what we offered, so "that link was wrong" can retract it.
            from agent.retrieval_guard import _URL_RE as _urlre
            self._last_answer_urls = _urlre.findall(final_response)
        except Exception as _url_exc:
            print(f"[AgentV2] |  (URL grounding skipped: {_url_exc})")

        # ── Say where the answer actually came from ──
        # A partial verdict means the content came from a different surface than
        # the request named — typically a screenshot standing in for the page
        # when the browser extension had nothing to give. That note was only
        # emitted as a progress line, so the answer itself still read like a
        # confident reading of the page, complete with a "Source URL" header.
        # Attach it to the answer, which is the part the user keeps.
        try:
            notes = list(dict.fromkeys(getattr(self, "_partial_retrieval_notes", []) or []))
            if notes and final_response and not plan.has_failed():
                caveat = " ".join(n.rstrip(".") + "." for n in notes[:2])
                final_response = (
                    final_response.rstrip()
                    + f"\n\n_Note: {caveat} Treat the details above as what was visible "
                      f"on screen, not a verified reading of the page._"
                )
                print(f"[AgentV2] |  answer carries {len(notes)} partial-retrieval note(s)")
        except Exception as _note_exc:
            print(f"[AgentV2] |  (partial note skipped: {_note_exc})")

        # ── Never present an answer that was never retrieved ──
        # Observed: every retrieval failed on quota, and the reply was still
        # "Here is the current weather in Patna." — a sentence that promises
        # content, followed by none. If nothing was retrieved, say why.
        try:
            from agent.retrieval_guard import (retrieval_attempted, retrieval_succeeded,
                                               last_failure_reason)
            if retrieval_attempted() and not retrieval_succeeded():
                body = (final_response or "").strip()
                promises = _re.search(
                    r"(?i)(?:here (?:is|are)|here'?s|below (?:is|are)|"
                    r"the (?:current|latest))", body)
                # A short promise with no substance behind it is the failure case.
                if promises and len(body) < 220:
                    reason = last_failure_reason() or "the page content could not be read"
                    final_response = (
                        f"I couldn't get that — {reason.rstrip('.')}. "
                        f"Nothing below this line was actually retrieved, so I'd rather "
                        f"not guess. Try again, or open the page and ask me to read it."
                    )
                    print("[AgentV2] |  replaced an unsupported answer with the real reason")
        except Exception as _hon_exc:
            print(f"[AgentV2] |  (answer honesty check skipped: {_hon_exc})")

        # ── Send final response ──
        try:
            await tool_registry.execute("send_response", {"message": final_response})
        except StopAsyncIteration:
            pass
        except Exception as e:
            print(f"[AgentV2] send_response error: {e}")

        if ws_callback and final_response and not response_emitted_to_ui:
            display_text = final_response.replace("[CONVERSATION_MODE_ON]", "").replace("[CONVERSATION_MODE_OFF]", "")
            display = "pill" if len(display_text) <= 40 else "card"
            await ws_callback({
                "type": "response",
                "payload": {
                    "text": display_text,
                    "display": display,
                    "app": context.active_app.lower() if context.active_app else "",
                },
            })

        self.conversation.add_model(final_response)

        # ── Proactive follow-up (conversation mode only) ──
        # Occasionally hint the user can keep talking
        if getattr(self, '_conversation_mode', False) and plan.is_complete() and _random.random() < 0.35:
            followup = self._suggest_followup(final_response, user_text)
            if followup:
                final_response = f"{final_response}\n\n{followup}"

        # Log research summary
        research_summary = self.working_memory.get_research_summary()
        if research_summary:
            print(f"\n[AgentV2] ═══ Research Summary ═══")
            print(research_summary)
            print(f"[AgentV2] ═══ End Research Summary ═══\n")

        return (final_response, False)

    def _looks_like_research_doc_task(self, user_text: str, task_summary: str) -> bool:
        """Delegate to the planner's canonical implementation."""
        if self.planner:
            return self.planner._is_research_document_request(user_text, task_summary)
        # Fallback: inline heuristic when planner not yet initialized
        lower = (user_text or "").lower()
        return any(k in lower for k in ("write a document", "create a document", "write a report", "make a doc"))

    def _build_research_stream_lines(self, content_text: str, max_lines: int = 3) -> list[str]:
        lines: list[str] = []
        for raw_line in str(content_text or "").splitlines():
            compact = " ".join(raw_line.split())
            if not compact:
                continue
            while compact and len(lines) < max_lines:
                if len(compact) <= 220:
                    lines.append(compact)
                    compact = ""
                    break
                split_at = compact.rfind(" ", 0, 220)
                if split_at < 80:
                    split_at = 220
                lines.append(compact[:split_at].rstrip())
                compact = compact[split_at:].lstrip()
            if len(lines) >= max_lines:
                break

        if not lines:
            compact = " ".join(str(content_text or "").split())
            if compact:
                lines.append(compact[:220])
        return lines[:max_lines]

    def _emit_research_stream(
        self,
        step: ExecutionStep,
        source_url: str,
        source_title: str,
        content_text: str,
        element_count: int,
    ) -> None:
        print(
            f"[ResearchStream] tool={step.tool} "
            f"source={source_url or 'current page'} "
            f"items={int(element_count or 0)}"
        )
        if source_title:
            print(f"[ResearchStream] title={source_title}")
        for idx, line in enumerate(self._build_research_stream_lines(content_text), 1):
            print(f"[ResearchStream] snippet[{idx}] {line}")

    def _log_research_content(
        self,
        step: ExecutionStep,
        result: str,
        is_research: bool,
        *,
        emit_stream: bool = True,
        commit_to_memory: bool = True,
    ) -> None:
        """Log detailed research content from browser_read_page and similar tools."""
        if not result:
            return
        raw_result = str(result).strip()
        if raw_result.startswith("Opened http") or raw_result.startswith("Opened web search"):
            return

        source_url = ""
        source_title = ""
        content_text = ""
        element_count = 0

        try:
            data = json.loads(result)
            if isinstance(data, dict):
                source_url = data.get("url", "")
                source_title = data.get("title", "")
                element_count = data.get("element_count", 0) or data.get("item_count", 0) or data.get("paragraph_count", 0)
                target_type = str(data.get("target_type", "")).strip().lower()
                content_text = data.get("content", "")

                # For fetch_web_content results
                if not content_text:
                    content_text = data.get("text", data.get("summary", ""))

                # For structured extraction tools
                if isinstance(data.get("items"), list) and (
                    not content_text or target_type in {"search_results", "structured_data"}
                ):
                    preview_rows: list[str] = []
                    for item in data.get("items", [])[:8]:
                        if not isinstance(item, dict):
                            continue
                        label = str(item.get("label", "")).strip()
                        href = str(item.get("href", "")).strip()
                        context = str(item.get("context", "")).strip()
                        row = " | ".join(part for part in (label, context, href) if part)
                        if row:
                            preview_rows.append(row[:220])
                    if preview_rows:
                        content_text = "\n".join(preview_rows)

                # For page-summary tools
                if not content_text and isinstance(data.get("headings"), list):
                    heading_lines = []
                    for h in data.get("headings", [])[:6]:
                        if isinstance(h, dict):
                            text = str(h.get("text", "")).strip()
                            if text:
                                heading_lines.append(text[:140])
                    if heading_lines:
                        page_type = str(data.get("page_type", "unknown")).strip()
                        content_text = f"Page type: {page_type}\n" + "\n".join(heading_lines)
        except (json.JSONDecodeError, TypeError):
            content_text = result[:500] if len(result) >= 60 else ""

        # Log what the agent is reading
        if content_text:
            preview = content_text[:300].replace("\n", " ↵ ")
            if emit_stream:
                print(f"[AgentV2] 📖 READING from: {source_url or 'current page'}")
                print(f"[AgentV2] 📖 Page title: {source_title or '(untitled)'}")
                print(f"[AgentV2] 📖 Elements read: {element_count}")
                print(f"[AgentV2] 📖 Content preview: {preview}{'...' if len(content_text) > 300 else ''}")
                print(f"[AgentV2] 📖 Content length: {len(content_text)} chars")
                self._emit_research_stream(step, source_url, source_title, content_text, element_count)

            # Store as research snippet in working memory (with quality filtering)
            if commit_to_memory and (is_research or len(content_text) >= 100):
                # Skip search engine homepages and browser navigation chrome
                _src_domain = self._domain_key(source_url)
                _junk_domains = {"google.com", "bing.com", "duckduckgo.com", "yahoo.com", "baidu.com"}
                _is_search_page = (
                    _src_domain in _junk_domains
                    or "/webhp" in source_url
                    or "/search?" in source_url
                    or source_url.rstrip("/").endswith(("google.com", "bing.com"))
                )
                _is_chrome_content = content_text.count("[mw_") > 3

                if _is_search_page or _is_chrome_content:
                    print(f"[AgentV2] 📖 Skipping non-research content from {_src_domain or 'unknown'}")
                    reason = "search_page" if _is_search_page else "browser_chrome"
                    print(f"[ResearchStream] stored=false reason={reason}")
                else:
                    was_stored = self.working_memory.log_research_snippet(
                        source=source_url,
                        title=source_title,
                        content=content_text,
                        tool=step.tool,
                    )
                    snippet_count = len(self.working_memory.get_research_snippets())
                    if was_stored:
                        print(f"[AgentV2] 📚 Research stored: {snippet_count} snippet(s) total")
                        print(f"[ResearchStream] stored=true snippets={snippet_count}")
                    else:
                        print(f"[AgentV2] 📚 Research store skipped by working-memory filter")
                        print(f"[ResearchStream] stored=false reason=memory_filter")
        elif emit_stream:
            print(f"[AgentV2] 📖 Page read returned no extractable content (url={source_url})")
            print(f"[ResearchStream] tool={step.tool} empty=true source={source_url or 'current page'}")

    def _extract_research_text(self, tool_result: str) -> str:
        if not tool_result:
            return ""
        raw = str(tool_result).strip()
        if not raw or raw.startswith("ERROR"):
            return ""
        if raw.startswith("Opened http"):
            return ""

        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                target_type = str(data.get("target_type", "")).strip().lower()
                source_url = str(data.get("url", "")).strip()
                if target_type == "search_results":
                    return ""
                if source_url and (
                    self._domain_key(source_url) in {"google.com", "bing.com", "duckduckgo.com", "yahoo.com"}
                    or "/search?" in source_url
                ):
                    return ""
                if isinstance(data.get("content"), str) and data["content"].strip():
                    prefix = []
                    if data.get("title"):
                        prefix.append(f"Title: {data['title']}")
                    if data.get("url"):
                        prefix.append(f"URL: {data['url']}")
                    prefix_text = "\n".join(prefix)
                    return (prefix_text + "\n" + data["content"]).strip()

                items = data.get("items")
                if isinstance(items, list) and items:
                    lines = []
                    if data.get("title"):
                        lines.append(f"Title: {data.get('title')}")
                    if data.get("url"):
                        lines.append(f"URL: {data.get('url')}")
                    for item in items[:10]:
                        if not isinstance(item, dict):
                            continue
                        label = str(item.get("label", "")).strip()
                        context = str(item.get("context", "")).strip()
                        href = str(item.get("href", "")).strip()
                        row = " | ".join(part for part in (label, context, href) if part)
                        if row:
                            lines.append(f"- {row[:240]}")
                    if len(lines) > 2:
                        return "\n".join(lines)

                for key in ("text", "summary", "message"):
                    value = data.get(key)
                    if isinstance(value, str) and len(value.strip()) >= 40:
                        return value.strip()
        except (json.JSONDecodeError, TypeError):
            pass

        return raw if len(raw) >= 60 else ""

    def _collect_research_snippets(self, step_results: dict) -> list[str]:
        snippets: list[str] = []
        seen_source_keys: set[str] = set()
        seen_content_keys: set[str] = set()

        def _remember(snippet_text: str, source_key: str = "") -> bool:
            normalized = " ".join(str(snippet_text or "").split()).lower()[:800]
            if not normalized:
                return False
            if source_key and source_key in seen_source_keys:
                return False
            if normalized in seen_content_keys:
                return False
            if source_key:
                seen_source_keys.add(source_key)
            seen_content_keys.add(normalized)
            return True

        # First, collect from working memory (more structured, with source info)
        wm_snippets = self.working_memory.get_research_snippets()
        for ws in wm_snippets:
            content = ws.get("content", "")
            source = ws.get("source", "")
            title = ws.get("title", "")
            if not content or len(content.strip()) < 40:
                continue
            header_parts = []
            if title:
                header_parts.append(f"Title: {title}")
            if source:
                header_parts.append(f"URL: {source}")
            header = "\n".join(header_parts)
            snippet = (header + "\n" + content).strip()[:3200]
            source_key = source.strip().rstrip("/").lower()
            if _remember(snippet, source_key=source_key):
                snippets.append(snippet)
            if len(snippets) >= 6:
                break

        # Supplement from step_results if needed
        if len(snippets) < 4:
            for _, result in sorted(step_results.items(), key=lambda item: item[0]):
                raw_result = str(result)
                snippet = self._extract_research_text(raw_result)
                if not snippet:
                    continue
                source_key = ""
                try:
                    data = json.loads(raw_result)
                    if isinstance(data, dict):
                        source_key = str(data.get("url", "")).strip().rstrip("/").lower()
                except (json.JSONDecodeError, TypeError):
                    source_key = ""
                snippet = snippet[:3200]
                if _remember(snippet, source_key=source_key):
                    snippets.append(snippet)
                if len(snippets) >= 6:
                    break

        if snippets:
            print(f"[AgentV2] 📚 Collected {len(snippets)} research snippet(s) for document synthesis")

        return snippets

    async def _synthesize_research_body(
        self,
        provider: LLMProvider,
        user_text: str,
        task_summary: str,
        snippets: list[str],
        for_response: bool = False,
    ) -> str:
        snippet_text = "\n\n".join(
            f"[Snippet {idx + 1}]\n{snippet}" for idx, snippet in enumerate(snippets)
        )
        if for_response:
            # Conversational summary — returned directly to the user via send_response
            prompt = (
                f"The user asked: {user_text or task_summary}\n\n"
                "Using ONLY the research snippets below, write a clear and engaging summary "
                "to present to the user. Cover: what it is, key facts, background context, "
                "and anything interesting or directly relevant to the user's question. "
                "Use **bold** section labels but keep the tone conversational. "
                "Return only the summary — no preamble, no meta-commentary.\n\n"
                f"{snippet_text}"
            )
            system_prompt = (
                "You are a knowledgeable research assistant. "
                "Present findings clearly, concisely, and engagingly."
            )
        else:
            # Full document — written to a file or Google Doc
            prompt = (
                "Write a complete, factual document from the research snippets.\n"
                f"Task: {user_text or task_summary}\n\n"
                "Requirements:\n"
                "- Use clear section headings.\n"
                "- Cover major system categories, how they work, and key tradeoffs.\n"
                "- Keep claims grounded in the snippets.\n"
                "- Return only the document text (markdown allowed).\n\n"
                f"{snippet_text}"
            )
            system_prompt = "You are an expert research analyst. Produce concise, accurate prose."
        response = await provider.generate(
            messages=[{"role": "user", "parts": [{"text": prompt}]}],
            system_prompt=system_prompt,
            tools=[],
            temperature=0.2,
        )
        text = (response.text or "").strip() if response else ""
        if text.startswith("```"):
            text = text.strip("`")
        return text[:14000]

    # Words that say which surface the answer has to come from. Used only to
    # flag a mismatch (UI-tree chrome standing in for source code), never to
    # pick a tool — selection stays in tools/selector.py.
    _EDITOR_CUES = ("code", "function", "class ", "file", "script", "snippet",
                    "vs code", "vscode", "editor", "refactor", "bug")
    _BROWSER_CUES = ("page", "tab", "website", "article", "url", "browser")

    # Only requests that ask *about* content need content back. "Open the File
    # menu" names a file but wants a click, and demanding source code from that
    # step's UI tree would fail a step that was working correctly.
    _CONTENT_INTENT = ("explain", "summar", "describe", "analy", "review",
                       "complexity", "what does", "what's in", "what is in",
                       "tell me about", "walk me through", "understand",
                       "how does", "read ")
    _ACTION_INTENT = ("open ", "click", "type ", "close ", "save ", "launch ",
                      "start ", "press ", "scroll", "navigate to")
    # Verbs that keep a content request a content request even when the user
    # also asks for something to be opened first.
    _CONTENT_OVERRIDE = ("explain", "summar", "complexity", "analy")

    # The user telling us a result was wrong is evidence about the *previous*
    # answer, not a request to start the whole hunt again from a search engine.
    _CORRECTION_CUES = ("wrong", "incorrect", "not right", "not correct",
                        "didn't work", "didnt work", "doesn't work", "doesnt work",
                        "that's not", "thats not", "wrong link", "bad link",
                        "not the right", "different link")

    # Tools whose result is only meaningful for the surface it came from.
    _BROWSER_RETRIEVAL = {"browser_read_page", "browser_read_text", "browser_snapshot",
                          "read_page_content", "get_page_summary", "extract_structured_data"}
    _EDITOR_RETRIEVAL = {"read_active_editor_file", "read_file"}

    def _context_mismatch(self, step: ExecutionStep, result: str) -> str:
        """
        Why this result does not belong to the bound context, or "".

        Deliberately narrow: it compares *identity* (which tab, which file),
        never content quality — that is the retrieval guard's job.
        """
        ctx = getattr(self, "_current_context", None)
        if ctx is None or ctx.ambiguous:
            return ""

        if step.tool in self._BROWSER_RETRIEVAL and ctx.is_browser and ctx.tab_id:
            try:
                from browser.store import browser_store
                live = browser_store.get_snapshot()
            except Exception:
                live = None
            live_tab = str(getattr(live, "tab_id", "") or "") if live else ""
            if live_tab and live_tab != ctx.tab_id:
                return (f"That content came from a different browser tab than the one "
                        f"you had in front when you asked.")

        if step.tool in self._EDITOR_RETRIEVAL and ctx.is_editor and ctx.file_path:
            wanted = str(step.args.get("path") or "").replace("\\", "/").lower()
            have = ctx.file_path.replace("\\", "/").lower()
            if wanted and not (wanted.endswith(have) or have.endswith(wanted)):
                return (f"That file is not the one open in {ctx.app} "
                        f"({ctx.file_name or 'the active editor'}).")
        return ""

    def _replan_unless_provider_dead(self, is_research: bool):
        """Re-planning callback, or None once the provider has terminally failed."""
        if not self.planner or is_research:
            return None

        async def _replan(*args, **kwargs):
            try:
                from agent import milestone_executor as _mx
                if getattr(_mx._instance, "_fatal_provider_error", ""):
                    print("[AgentV2] |  skipping replan: provider terminally failed")
                    return None
            except Exception:
                pass
            return await self.planner.replan_remaining(*args, **kwargs)

        return _replan

    @classmethod
    def _is_correction(cls, user_text: str) -> bool:
        low = " ".join((user_text or "").lower().split())
        return any(cue in low for cue in cls._CORRECTION_CUES)

    # Navigation tools whose target must be a real, retrieved destination.
    _NAVIGATION_TOOLS = {"open_url", "browser_switch_tab"}

    # Phrasings that say "the link that is in that content", as opposed to a
    # destination the user typed themselves.
    _LINK_FROM_CONTENT = (
        "link in", "link from", "link to the", "link on", "url in", "url from",
        "mentioned in", "in this email", "in the email", "in this mail",
        "in the mail", "on this page", "in this message", "in the message",
    )

    def _ungrounded_navigation(self, step: ExecutionStep, user_text: str) -> str:
        """Refusal message when a navigation target was never actually retrieved.

        Returns "" when the step may proceed. Deliberately narrow: it only
        applies when the request asked for a link *out of* some content, so
        "open youtube.com" and ordinary web_search navigation are untouched.
        """
        if step.tool not in self._NAVIGATION_TOOLS:
            return ""
        low = " ".join((user_text or "").lower().split())
        if not any(cue in low for cue in self._LINK_FROM_CONTENT):
            return ""
        target = str(step.args.get("url") or "").strip()
        if not target:
            return ""
        try:
            from agent.retrieval_guard import ungrounded_urls
            if not ungrounded_urls(target):
                return ""
        except Exception:
            return ""          # guard broken → do not block the user's task
        return ("I couldn't find that link in the page content I actually read, so I "
                "haven't opened anything — the address would have been a guess. "
                "Open the message you mean and make sure the ClarityOS browser "
                "extension is connected, then ask again.")

    def _validate_retrieval(self, step: ExecutionStep, result: str, user_text: str):
        """Classify a tool result and record the verdict for progress reporting.

        Returns the Verdict, or None if the guard itself could not run — a
        broken guard must not take the task down with it.
        """
        try:
            from agent import retrieval_guard as _rg
            verdict = _rg.classify(
                step.tool, result,
                expected_source=self._expected_retrieval_source(user_text),
            )
            # Identity binding: content must come from the window/tab/file this
            # request was bound to. A fresh get_ui_tree of a *different* app is
            # a valid tool result and still the wrong answer.
            if verdict.ok:
                mismatch = self._context_mismatch(step, result)
                if mismatch:
                    verdict = _rg.Verdict(False, verdict.source, mismatch,
                                          meta=verdict.meta)

            _rg.note_verdict(verdict)
            if verdict.ok:
                # Only validated content can ground a URL in the answer.
                _rg.note_content(result)
            if verdict.partial:
                self._partial_retrieval_notes.append(verdict.reason)
            # ASCII only: these lines also run from the guard's own failure
            # path, and a UnicodeEncodeError on a cp1252 console would
            # otherwise disable validation silently.
            state = "rejected" if not verdict.ok else ("partial" if verdict.partial else "ok")
            if state != "ok":
                print(f"[AgentV2] |  retrieval {state} ({verdict.source}): {verdict.reason[:120]}")
            return verdict
        except Exception as exc:                 # never let validation kill a task
            try:
                print(f"[AgentV2] |  (retrieval guard skipped: {exc})")
            except Exception:
                pass
            return None

    def _expected_retrieval_source(self, user_text: str) -> str:
        low = " ".join((user_text or "").lower().split())
        if not any(cue in low for cue in self._CONTENT_INTENT):
            return ""
        if (any(cue in low for cue in self._ACTION_INTENT)
                and not any(cue in low for cue in self._CONTENT_OVERRIDE)):
            return ""
        if any(cue in low for cue in self._EDITOR_CUES):
            return "editor"
        if any(cue in low for cue in self._BROWSER_CUES):
            return "browser"
        return ""

    def _remember_opened_url(self, step: ExecutionStep, result: str) -> None:
        if step.tool == "open_url":
            candidate = str(step.args.get("url") or "").strip()
            if not candidate:
                import re
                match = re.search(r"https?://[^\s\"']+", str(result))
                if match:
                    candidate = match.group(0).rstrip(".,)")
            if candidate:
                self._last_opened_url = candidate
        elif step.tool == "web_search":
            # web_search opens a Google search URL — track it so the
            # browser_read_page recovery can detect tab mismatches.
            query = str(step.args.get("query", "")).strip()
            if query:
                from urllib.parse import quote_plus
                self._last_opened_url = f"https://www.google.com/search?q={quote_plus(query)}"

    def _domain_key(self, url: str) -> str:
        if not url:
            return ""
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            return ""
        if host.startswith("www."):
            host = host[4:]
        return host

    async def _recover_browser_context(self, step: ExecutionStep, result: str) -> str:
        browser_read_tools = {
            "browser_read_page",
            "browser_read_text",
            "read_page_content",
            "extract_structured_data",
            "get_page_summary",
        }
        if step.tool not in browser_read_tools or not self._last_opened_url:
            return result

        expected_url = self._last_opened_url

        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return result
        if not isinstance(payload, dict):
            return result
        observed_url = str(payload.get("url") or "").strip()
        if not observed_url:
            return result

        expected_domain = self._domain_key(expected_url)
        observed_domain = self._domain_key(observed_url)
        if expected_domain and observed_domain and expected_domain == observed_domain:
            return result

        print(
            f"[AgentV2] {step.tool} context drift detected "
            f"(expected {expected_domain or expected_url}, got {observed_domain or observed_url}); retrying."
        )
        switched = False
        for switch_target in (expected_url, expected_domain):
            if not switch_target:
                continue
            try:
                switch_result = await tool_registry.execute("browser_switch_tab", {"url": switch_target})
                try:
                    switch_payload = json.loads(switch_result)
                    switched = bool(switch_payload.get("ok"))
                except (TypeError, json.JSONDecodeError):
                    switched = "switched" in str(switch_result).lower()
            except Exception as e:
                print(f"[AgentV2] browser_switch_tab recovery failed: {e}")
                switched = False
            if switched:
                break

        if not switched:
            return result

        try:
            await tool_registry.execute("browser_refresh_refs", {})
        except Exception as e:
            print(f"[AgentV2] browser_refresh_refs during recovery failed: {e}")

        retry_args = dict(step.args)
        if step.tool in ("browser_read_page", "browser_read_text"):
            retry_args["refresh"] = True
        try:
            retry_result = await tool_registry.execute(step.tool, retry_args)
        except Exception as e:
            print(f"[AgentV2] {step.tool} recovery retry failed: {e}")
            return result

        return retry_result

    async def _execute_step(
        self,
        step: ExecutionStep,
        world_state: WorldState,
        ws_callback: Optional[WSCallback],
        user_text: str = "",
        task_summary: str = "",
    ) -> bool:
        """
        Execute a single step and verify the result.
        
        Returns:
            True if step succeeded, False if failed
        """
        t_start = _time.time()
        print(f"[AgentV2] ┌─ EXECUTE: {step.tool}")
        print(f"[AgentV2] │  Args: {json.dumps(step.args, ensure_ascii=False)[:300]}")
        
        try:
            gateway_context = self._current_tool_gateway_context()
            set_tool_gateway_context(
                active_app=world_state.active_app,
                browser_url=self._last_opened_url or world_state.browser_url or "",
                background_mode=False,
                browser_bridge_connected=gateway_context["browser_bridge_connected"],
                browser_has_snapshot=gateway_context["browser_has_snapshot"],
                browser_session_id=gateway_context["browser_session_id"],
            )
            browser_read_tools = {
                "browser_read_page",
                "browser_read_text",
                "read_page_content",
                "extract_structured_data",
                "get_page_summary",
            }
            # Before browser reads: verify snapshot matches real browser URL
            # Only check if the snapshot is stale (>3s) — post-nav refresh already covers fresh cases
            if step.tool in browser_read_tools and not step.args.get("refresh"):
                try:
                    from browser.store import browser_store as _bstore
                    _snap = _bstore.get_snapshot()
                    _snap_age = (_time.time() - float(_snap.timestamp or 0)) if _snap else 999
                    if _snap_age > 3.0:
                        _rd_app = await perception.get_active_app()
                        if _rd_app.lower() in perception.BROWSERS:
                            _rd_url = await perception.get_browser_url(_rd_app)
                            if _rd_url and _snap and self._domain_key(_rd_url) != self._domain_key(_snap.url or ""):
                                if step.tool in ("browser_read_page", "browser_read_text"):
                                    step.args["refresh"] = True
                                else:
                                    await tool_registry.execute("browser_refresh_refs", {})
                                print(f"[AgentV2] 🔄 Auto-refresh: browser={self._domain_key(_rd_url)}, "
                                      f"snapshot={self._domain_key(_snap.url or '')}")
                except Exception:
                    pass

            # The user's explicit browser choice is theirs, not the planner's.
            # open_url otherwise falls through to webbrowser.open(), i.e. the
            # system default, so "open it in Chrome" silently became Edge.
            if step.tool == "open_url" and not str(step.args.get("browser") or "").strip():
                try:
                    from tools.browser_target import resolve_target
                    ctx = getattr(self, "_current_context", None)
                    foreground = getattr(ctx, "browser", "") if ctx else ""
                    named = resolve_target(user_text, foreground)
                    if named:
                        step.args["browser"] = named
                        why = ("named by the user" if named != foreground
                               else "the browser in front")
                        print(f"[AgentV2] |  browser target: {named} ({why})")
                except Exception:
                    pass

            # When the user asked for "the link in this email/page", the URL has
            # to come from that content. Grounding was only checked on the final
            # answer text, so a URL invented by the model could still be handed
            # straight to open_url — which is how paypal-ai.devpost.com (a 404)
            # got opened. Navigating is the irreversible part, so it is checked
            # here, before the tool runs, not after.
            blocked = self._ungrounded_navigation(step, user_text)
            if blocked:
                step.status = StepStatus.FAILED
                step.error = blocked
                print(f"[AgentV2] |  Step {step.id} BLOCKED (ungrounded URL): {blocked[:120]}")
                if ws_callback:
                    await ws_callback({"type": "doing", "text": blocked,
                                       "tool": step.tool, "variant": "failed"})
                return False

            # Execute the tool
            result = await tool_registry.execute(step.tool, step.args)
            elapsed = _time.time() - t_start
            self._remember_opened_url(step, result)

            print(f"[AgentV2] │  Duration: {elapsed:.2f}s")

            # After web_search: wait for the browser tab to switch and force a
            # snapshot refresh so the next browser_read_page reads the search
            # results instead of whatever tab was previously active.
            if step.tool == "web_search":
                # Wait for a new snapshot instead of a fixed sleep
                from browser.bridge import browser_bridge as _bb
                from browser.store import browser_store as _bstore
                _pre_snap = _bstore.get_snapshot()
                _pre_gen = _pre_snap.generation if _pre_snap else 0
                try:
                    await tool_registry.execute("browser_refresh_refs", {})
                except Exception:
                    pass  # Non-critical
                await _bb.wait_for_snapshot(min_generation=_pre_gen, timeout=2.0)

            # After click navigation: wait for page to load and refresh snapshot
            # so the next read step sees the new page, not stale pre-click data.
            should_refresh_after_nav = step.tool in ("browser_click_match", "browser_click_ref", "open_url")
            if step.tool == "find_and_act" and str(step.args.get("action", "")).lower() == "click":
                should_refresh_after_nav = True
            if should_refresh_after_nav:
                # Event-driven: wait for a snapshot newer than pre-action, with short fallback
                from browser.bridge import browser_bridge as _bb2
                from browser.store import browser_store as _bstore2
                _nav_pre_snap = _bstore2.get_snapshot()
                _nav_pre_gen = _nav_pre_snap.generation if _nav_pre_snap else 0
                try:
                    await tool_registry.execute("browser_refresh_refs", {})
                except Exception:
                    pass
                await _bb2.wait_for_snapshot(min_generation=_nav_pre_gen, timeout=2.0)
                # Update tracked URL to the REAL browser URL after navigation.
                # This prevents drift detection from trying to navigate BACK to
                # the previous page (e.g. Google search results).
                try:
                    _nav_app = await perception.get_active_app()
                    if _nav_app.lower() in perception.BROWSERS:
                        _nav_url = await perception.get_browser_url(_nav_app)
                        if _nav_url:
                            self._last_opened_url = _nav_url
                except Exception:
                    pass

            result = await self._recover_browser_context(step, result)
            
            # Log tool output (truncated for readability)
            result_preview = result[:200].replace("\n", " ↵ ") if result else "(empty)"
            print(f"[AgentV2] │  Output: {result_preview}{'…' if len(result) > 200 else ''}")
            print(f"[AgentV2] │  Output length: {len(result)} chars")

            # ── Retrieval validation ──────────────────────────────────────
            # A tool call that *ran* is not the same as content that was
            # *retrieved*. Chrome's profile chooser, a UI tree holding only
            # window chrome, and a 58-character "ERROR: that window title does
            # not identify an editor file" all used to reach summarization and
            # be described as though they were the thing the user asked about.
            # Both agents share this executor, so validating here covers the
            # LangGraph path too.
            _verdict = self._validate_retrieval(step, result, user_text)
            if _verdict is not None and not _verdict.ok:
                # Marking the step FAILED here is the whole point. Rewriting the
                # result to an "ERROR: …" string was not enough: self.verifier
                # only recognises that prefix for a few tools, so for get_ui_tree
                # it still returned success and the refusal text was stored as
                # step.result — i.e. handed to the LLM as the retrieved content.
                step.status = StepStatus.FAILED
                step.error = _verdict.user_message()
                if step.tool not in ("send_response", "await_reply"):
                    self.working_memory.log_action(
                        tool=step.tool, args=step.args,
                        result=step.error, success=False,
                    )
                print(f"[AgentV2] |  Step {step.id} FAILED (retrieval): {step.error[:120]}")
                if ws_callback:
                    await ws_callback({
                        "type": "doing", "text": step.error,
                        "tool": step.tool, "variant": "failed",
                    })
                return False
            if (_verdict is not None and _verdict.partial and _verdict.reason
                    and not self._partial_note_emitted):
                # Say once that the content is limited, rather than letting the
                # answer imply the whole file or page was read.
                self._partial_note_emitted = True
                if ws_callback:
                    await ws_callback({
                        "type": "doing", "text": _verdict.reason,
                        "tool": step.tool, "variant": "partial",
                    })

            # ── Research logging & content extraction ──
            is_research = self._looks_like_research_doc_task(user_text, task_summary)
            research_content_tools = {
                "browser_read_page",
                "browser_read_text",
                "fetch_web_content",
                "web_scrape",
                "get_web_information",
                "gworkspace_analyze",
                "gdocs_read",
                "read_page_content",
                "extract_structured_data",
                "get_page_summary",
            }
            if step.tool in research_content_tools:
                self._log_research_content(
                    step,
                    result,
                    is_research,
                    emit_stream=True,
                    commit_to_memory=False,
                )
            elif step.tool == "web_search" and is_research:
                query = step.args.get("query", "")
                print(f"[AgentV2] 🔍 RESEARCH SEARCH: '{query}'")
            elif step.tool == "browser_click_match" and is_research:
                query = step.args.get("query", "")
                print(f"[AgentV2] 🖱️ NAVIGATING to source: clicking '{query}'")
            elif step.tool == "browser_scroll" and is_research:
                direction = step.args.get("direction", "down")
                print(f"[AgentV2] 📜 SCROLLING {direction} for more research content")
            
            # Handle special response tools
            if step.tool == "send_response" and result.startswith("RESPONSE:"):
                raw = result[len("RESPONSE:"):]
                try:
                    modal_data = json.loads(raw)
                    step.result = modal_data.get("message", raw)
                    step.modal_data = modal_data  # structured modal payload
                except (json.JSONDecodeError, TypeError):
                    step.result = raw
                    step.modal_data = None
                step.status = StepStatus.COMPLETED
                return True
            
            if step.tool == "await_reply" and result.startswith("AWAIT:"):
                raw = result[len("AWAIT:"):]
                try:
                    modal_data = json.loads(raw)
                    step.result = modal_data.get("message", raw)
                    step.modal_data = modal_data
                except (json.JSONDecodeError, TypeError):
                    step.result = raw
                    step.modal_data = None
                step.status = StepStatus.COMPLETED
                return True
            
            # Verify the result (visual verification for UI-mutating tools)
            verification = await self.verifier.verify_with_visual(
                tool_name=step.tool,
                tool_args=step.args,
                tool_result=result,
                success_criteria=step.success_criteria,
                get_current_state=self._get_quick_state,
                get_visual_state=self._get_visual_state,
            )
            
            print(f"[AgentV2] │  Verification: success={verification.success}, "
                  f"confidence={verification.confidence:.0%}, msg={verification.message}")

            verification_success = verification.success
            if verification.confidence < 0.5:
                verification_success = False
                if not verification.should_retry:
                    verification.should_retry = True
                verification.message = (
                    f"Verification confidence too low ({verification.confidence:.0%}): "
                    f"{verification.message}"
                )

            if verification_success:
                if step.tool in research_content_tools:
                    self._log_research_content(
                        step,
                        result,
                        is_research,
                        emit_stream=False,
                        commit_to_memory=True,
                    )
                if step.tool not in ("send_response", "await_reply"):
                    self.working_memory.log_action(
                        tool=step.tool,
                        args=step.args,
                        result=result,
                        success=True,
                    )
                step.status = StepStatus.COMPLETED
                step.result = result
                total_elapsed = _time.time() - t_start
                print(f"[AgentV2] └─ ✓ Step {step.id} COMPLETED ({total_elapsed:.2f}s)")
                return True
            
            # Verification failed - try retry or fallback
            if verification.should_retry and step.retries < step.max_retries:
                print(f"[AgentV2] │  ↻ Retrying step {step.id} (attempt {step.retries + 1}/{step.max_retries})…")
                step.retries += 1
                step.status = StepStatus.PENDING  # Reset for retry

                # Adaptive retry: modify args for known failure patterns
                if step.tool == "type_in_field" and "no text field matching" in str(verification.message).lower():
                    # Try alternative field descriptions on retry
                    original_desc = step.args.get("field_description", "")
                    alt_descs = {
                        "Search": "Search or filter",
                        "search": "Search or filter",
                        "Search or filter": "text field",
                        "Message": "Type a message",
                        "message": "Type a message",
                    }
                    new_desc = alt_descs.get(original_desc)
                    if new_desc:
                        step.args["field_description"] = new_desc
                        print(f"[AgentV2] │  Adapted retry: field_description='{new_desc}'")

                if step.tool == "get_ui_tree" and "timed out" in str(verification.message).lower():
                    # On timeout retry, add a brief delay for app to settle
                    import asyncio as _asyncio
                    await _asyncio.sleep(1.0)
                    print(f"[AgentV2] │  Adapted retry: added 1s settle delay after timeout")

                return await self._execute_step(step, world_state, ws_callback, user_text=user_text, task_summary=task_summary)
            
            # Try fallback tool if available
            if step.fallback_tool:
                print(f"[AgentV2] │  Trying fallback tool: {step.fallback_tool}")
                fallback_result = await tool_registry.execute(
                    step.fallback_tool, 
                    step.fallback_args or step.args
                )
                
                fallback_verify = await self.verifier.verify(
                    tool_name=step.fallback_tool,
                    tool_args=step.fallback_args or step.args,
                    tool_result=fallback_result,
                    success_criteria=step.success_criteria
                )
                
                # The fallback is a second retrieval attempt and has to clear the
                # same bar as the first; without this it was the one way an
                # unvalidated payload could still reach summarization.
                fb_step = ExecutionStep(
                    id=step.id, tool=step.fallback_tool,
                    args=step.fallback_args or step.args,
                    description=f"fallback:{step.fallback_tool}",
                )
                fb_verdict = self._validate_retrieval(fb_step, fallback_result, user_text)
                if fallback_verify.success and (fb_verdict is None or fb_verdict.ok):
                    step.status = StepStatus.COMPLETED
                    step.result = fallback_result
                    return True
                if fb_verdict is not None and not fb_verdict.ok:
                    print(f"[AgentV2] |  Fallback retrieval rejected: {fb_verdict.reason[:120]}")
            
            # Step failed
            step.status = StepStatus.FAILED
            step.error = verification.message
            if step.tool not in ("send_response", "await_reply"):
                self.working_memory.log_action(
                    tool=step.tool, args=step.args,
                    result=verification.message, success=False,
                )
            print(f"[AgentV2] └─ ✗ Step {step.id} FAILED: {verification.message}")
            return False
            
        except StopAsyncIteration:
            # send_response in benchmark mode raises this - treat as success
            step.status = StepStatus.COMPLETED
            step.result = str(step.args.get("message") or step.args.get("response_text", "Done"))
            print(f"[AgentV2] └─ ✓ Step {step.id} COMPLETED (benchmark mode)")
            return True
        except Exception as e:
            print(f"[AgentV2] └─ ✗ Step {step.id} EXCEPTION: {e}")
            step.status = StepStatus.FAILED
            step.error = str(e)
            if step.tool not in ("send_response", "await_reply"):
                self.working_memory.log_action(
                    tool=step.tool, args=step.args,
                    result=str(e), success=False,
                )
            return False

    async def _get_quick_state(self) -> dict:
        """Get quick state for verification (minimal perception)."""
        try:
            active_app, window_title, clipboard = await asyncio.gather(
                perception.get_active_app(),
                perception.get_window_title(),
                perception.get_clipboard(),
            )
            browser_url = None
            if active_app.lower() in perception.BROWSERS:
                browser_url = await perception.get_browser_url(active_app)
            
            return {
                "active_app": active_app,
                "window_title": window_title,
                "browser_url": browser_url,
                "clipboard": (clipboard or "")[:500],
                "last_typed_text": self.working_memory.get_last_typed_text()[:500],
            }
        except Exception:
            return {}

    async def _get_visual_state(self) -> str:
        """Get DOM snapshot + screenshot summary for visual verification."""
        parts: list[str] = []
        try:
            from browser.store import browser_store
            snap = browser_store.get_snapshot()
            if snap and snap.elements:
                dom_lines = [f"Page: {snap.title} ({snap.url})"]
                for el in snap.elements[:30]:
                    label = el.primary_label()[:60] if el.primary_label() else el.tag
                    dom_lines.append(f"  [{el.ref_id}] {el.role or el.tag}: {label}")
                parts.append("\n".join(dom_lines))
        except Exception:
            pass

        try:
            screenshot_path = await perception.capture_screenshot()
            if screenshot_path:
                parts.append(f"Screenshot: {screenshot_path}")
        except Exception:
            pass

        return "\n".join(parts) if parts else ""


# ═══════════════════════════════════════════════════════════════
#  Factory Function
# ═══════════════════════════════════════════════════════════════

def create_agent(version: str = "v2", use_planning: bool = True, persist: bool = True):
    """
    Create the active ClarityOS agent runtime.
    
    Args:
        version: Deprecated compatibility argument. V2 is always used.
        use_planning: For V2, whether to use LLM planning (slower but smarter)
        persist: If True, persist conversation sessions to disk.
                Set False for benchmarks / tests.
        
    Returns:
        ClarityOSAgentV2 instance
    """
    if str(version or "v2").lower() != "v2":
        print(f"[AgentV2] Ignoring deprecated agent version '{version}' and using V2.")
    return ClarityOSAgentV2(use_planning=use_planning, persist=persist)
