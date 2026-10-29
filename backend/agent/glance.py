"""
Moonwalk — Glance: Lightweight Parallel Screen Perception
==========================================================

A fast, token-efficient screen awareness layer that runs alongside
the agent's tool execution loop.  Provides real-time UI element
positions, active app verification, and screen descriptions without
expensive LLM calls for most operations.

Architecture
------------
  peek()        → Cached accessibility data       (~0 ms if fresh, ~150 ms if stale)
  refresh()     → Force re-query accessibility API (~150-300 ms)
  deep_look()   → Screenshot + LLM vision         (~2-4 s, pivotal moments only)

Cost profile
------------
  peek/refresh : Zero tokens, zero API quota.  Pure local AppleScript.
  deep_look    : ~500 tokens via Gemini Vision.  Budget-capped per task (default 5).

Integration points
------------------
  1. MilestoneExecutor emits  {"type": "doing", "variant": "looking"}  at pivotal
     moments so the user sees the agent "looking" at their screen.
  2. Glance data is injected into the LLM's environment context so the model
     knows which UI elements (buttons, text fields) are available and where.
  3. After open_app, an automatic peek() verifies the target app actually reached
     the foreground — catches the Devpost-instead-of-WhatsApp scenario.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Any, Callable, Awaitable


# ═══════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════

@dataclass
class GlanceResult:
    """Snapshot of what the agent 'sees' right now."""

    active_app: str = ""
    window_title: str = ""
    elements: list[dict] = field(default_factory=list)
    text_fields: list[dict] = field(default_factory=list)
    buttons: list[dict] = field(default_factory=list)
    timestamp: float = 0.0

    # Only populated by deep_look()
    screen_description: str = ""
    is_deep: bool = False

    @property
    def element_count(self) -> int:
        return len(self.elements)

    def find_element(self, description: str) -> Optional[dict]:
        """Quick local search for an element by name (case-insensitive)."""
        desc_lower = description.lower()
        for el in self.elements:
            name = el.get("name", "").lower()
            if name and desc_lower in name:
                return el
        return None

    def summarize(self, max_elements: int = 12) -> str:
        """Brief text summary suitable for LLM context injection.

        Designed to be short enough (~200-400 chars) that it barely
        increases prompt token count while giving the LLM critical
        awareness of the screen layout.
        """
        lines = [f"👁 Glance — {self.active_app} | {self.window_title}"]

        if self.text_fields:
            names = [f.get("name", "?") for f in self.text_fields[:5]]
            lines.append(f"  Fields: {', '.join(names)}")

        if self.buttons:
            names = [b.get("name", "?") for b in self.buttons[:max_elements]]
            lines.append(f"  Buttons: {', '.join(names)}")

        if self.screen_description:
            lines.append(f"  Screen: {self.screen_description[:250]}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Tool categories for glance behaviour
# ═══════════════════════════════════════════════════════════════

# Tools that benefit from knowing available UI elements BEFORE execution
_GLANCE_BEFORE: frozenset[str] = frozenset({
    "click_ui", "type_in_field", "click_element", "get_ui_tree",
})

# Tools after which we should verify the screen changed
_GLANCE_AFTER: frozenset[str] = frozenset({
    "open_app",
})

# Input element roles (for text_fields classification)
_INPUT_ROLES: frozenset[str] = frozenset({
    "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField",
})

# Interactive element roles (for buttons classification)
_BUTTON_ROLES: frozenset[str] = frozenset({
    "AXButton", "AXLink", "AXMenuItem", "AXPopUpButton",
    "AXCheckBox", "AXRadioButton", "AXTab",
})


# ═══════════════════════════════════════════════════════════════
#  ScreenGlance — main class
# ═══════════════════════════════════════════════════════════════

class ScreenGlance:
    """Lightweight parallel perception engine.

    Most calls resolve from a 2-second cache — effectively free.
    LLM vision is only used at pivotal moments (app transitions,
    failure recovery) and is budget-capped per task.
    """

    def __init__(self, cache_ttl: float = 2.0, deep_look_budget: int = 5):
        self._cache: Optional[GlanceResult] = None
        self._cache_ttl = cache_ttl
        self._deep_look_cooldown = 8.0          # min seconds between deep looks
        self._last_deep_look_time = 0.0
        self._deep_look_count = 0
        self._deep_look_budget = deep_look_budget
        self._previous_app: str = ""             # tracks app transitions

    # ── Lifecycle ──

    def reset(self) -> None:
        """Reset for a new task.  Call at the start of each milestone plan."""
        self._cache = None
        self._last_deep_look_time = 0.0
        self._deep_look_count = 0
        self._previous_app = ""

    # ── Core perception methods ──

    async def peek(self) -> GlanceResult:
        """Return cached screen state.  Auto-refreshes if stale.

        Cost: ~0 ms (cache hit) or ~150 ms (cache miss, accessibility query).
        Tokens: Zero.
        """
        if self._cache and (time.time() - self._cache.timestamp) < self._cache_ttl:
            return self._cache
        return await self.refresh()

    async def refresh(self) -> GlanceResult:
        """Force re-query of macOS Accessibility API.

        Cost: ~150-300 ms (AppleScript subprocess).
        Tokens: Zero.
        """
        from agent.perception import get_active_app, get_window_title

        result = GlanceResult(timestamp=time.time())

        try:
            app_name, window_title = await asyncio.gather(
                get_active_app(),
                get_window_title(),
            )
            result.active_app = app_name
            result.window_title = window_title

            # Get UI elements from the shared cached tree system
            from tools.mac_tools import _get_cached_ui_tree
            elements, _ = await _get_cached_ui_tree(app_name=app_name)
            result.elements = elements

            # Classify elements for quick access
            result.text_fields = [e for e in elements if e.get("role") in _INPUT_ROLES]
            result.buttons = [e for e in elements if e.get("role") in _BUTTON_ROLES]

        except Exception as e:
            print(f"[Glance] ⚠ Refresh failed: {e}")

        self._cache = result
        return result

    async def deep_look(self, question: str = "") -> GlanceResult:
        """Full screenshot + Gemini Vision analysis.

        Cost: ~2-4 seconds, ~500 tokens.
        Budget: Capped at {deep_look_budget} per task.
        """
        # Budget guard
        if self._deep_look_count >= self._deep_look_budget:
            print(f"[Glance] ⚠ Deep look budget exhausted ({self._deep_look_budget})")
            return await self.refresh()

        # Cooldown guard
        elapsed = time.time() - self._last_deep_look_time
        if elapsed < self._deep_look_cooldown:
            print(f"[Glance] ⚠ Deep look cooldown ({elapsed:.1f}s < {self._deep_look_cooldown}s)")
            return await self.refresh()

        # Start with accessibility data
        result = await self.refresh()
        result.is_deep = True

        # Layer on LLM vision
        try:
            from tools.mac_tools import read_screen
            screen_text = await read_screen(
                question=question or (
                    "Briefly describe what's on screen. Which app is visible? "
                    "What are the main interactive elements (buttons, fields, menus)?"
                )
            )
            if screen_text and not screen_text.startswith("ERROR"):
                result.screen_description = screen_text
                self._last_deep_look_time = time.time()
                self._deep_look_count += 1
                print(f"[Glance] 👁 Deep look #{self._deep_look_count}: "
                      f"{screen_text[:80]}…")
            else:
                print(f"[Glance] ⚠ Deep look returned error: {(screen_text or '')[:100]}")
        except Exception as e:
            print(f"[Glance] ⚠ Deep look vision failed: {e}")

        self._cache = result
        return result

    # ── Decision helpers ──

    def should_deep_look(
        self,
        tool: str,
        tool_succeeded: bool,
        app_changed: bool,
    ) -> bool:
        """Heuristic: should we spend a deep_look right now?"""
        if self._deep_look_count >= self._deep_look_budget:
            return False
        if (time.time() - self._last_deep_look_time) < self._deep_look_cooldown:
            return False

        # After open_app — verify the right app is in front
        if app_changed:
            return True

        # After a UI interaction tool failed — need to see what went wrong
        if not tool_succeeded and tool in _GLANCE_BEFORE:
            return True

        return False

    def should_peek_before(self, tool: str) -> bool:
        """Should we inject a quick glance before this tool runs?"""
        return tool in _GLANCE_BEFORE

    def should_peek_after(self, tool: str) -> bool:
        """Should we glance after this tool to verify the result?"""
        return tool in _GLANCE_AFTER

    def detect_app_change(self, glance: GlanceResult) -> bool:
        """Check if the active app changed since last observation."""
        if not self._previous_app:
            self._previous_app = glance.active_app
            return False
        changed = (
            glance.active_app.lower() != self._previous_app.lower()
            and bool(glance.active_app)
        )
        self._previous_app = glance.active_app
        return changed

    # ── Context injection ──

    def build_context(self, tool: str, glance: GlanceResult) -> str:
        """Generate a brief context block for injection into the LLM env.

        This gives the model awareness of available UI elements without
        a full read_screen call.  Keeps it short to minimize token cost.
        """
        if not glance.elements:
            return ""

        lines: list[str] = [
            f"[Screen Glance — {glance.active_app}]",
        ]

        # For UI-targeting tools, list discoverable targets
        if tool in ("click_ui", "type_in_field", "click_element"):
            if glance.text_fields:
                field_list = ", ".join(
                    f'"{f.get("name", "?")}"'
                    for f in glance.text_fields[:6]
                    if f.get("name") and f["name"] != "unnamed"
                )
                if field_list:
                    lines.append(f"  Text fields available: {field_list}")

            if glance.buttons:
                btn_list = ", ".join(
                    f'"{b.get("name", "?")}"'
                    for b in glance.buttons[:10]
                    if b.get("name") and b["name"] != "unnamed"
                )
                if btn_list:
                    lines.append(f"  Buttons available: {btn_list}")
        else:
            # Generic summary
            if glance.element_count:
                lines.append(f"  UI elements: {glance.element_count}")
            if glance.text_fields:
                lines.append(f"  Text fields: {len(glance.text_fields)}")

        if glance.screen_description:
            lines.append(f"  Vision: {glance.screen_description[:200]}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Singleton
# ═══════════════════════════════════════════════════════════════

_glance: Optional[ScreenGlance] = None


def get_glance() -> ScreenGlance:
    """Get or create the singleton ScreenGlance instance."""
    global _glance
    if _glance is None:
        _glance = ScreenGlance()
    return _glance
