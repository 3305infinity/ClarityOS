"""
Moonwalk — Planner Types
========================
Runtime dataclasses for milestone planning and step execution primitives.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum


# ═══════════════════════════════════════════════════════════════
#  Step Status
# ═══════════════════════════════════════════════════════════════

class StepStatus(str, Enum):
    """Status of an execution step."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


# ═══════════════════════════════════════════════════════════════
#  Milestone (V3 planning unit)
# ═══════════════════════════════════════════════════════════════

class MilestoneStatus(str, Enum):
    """Status of a milestone."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Milestone:
    """
    A high-level goal checkpoint in a milestone-based plan.

    Unlike an ExecutionStep (which maps to one tool call), a Milestone
    describes a *goal* that the LLM micro-loop achieves by making
    as many tool calls as needed.  The planner only specifies WHAT
    must be true, not HOW to do it.

    Fields:
        id:              Sequential milestone number (1-based)
        goal:            What must be accomplished (natural language)
        success_signal:  Observable evidence that the goal is met
        hint_tools:      Suggested tool categories (not prescriptive)
        max_actions:     Deprecated legacy field (runtime now uses a hard safety cap)
        depends_on:      Milestone IDs that must finish first
        deliverable_key: Key to store the milestone's output under
                         in the working memory / step_results dict
    """
    id: int
    goal: str
    success_signal: str = ""
    hint_tools: List[str] = field(default_factory=list)
    max_actions: int = 0  # Deprecated (kept for backward compatibility only)
    depends_on: List[int] = field(default_factory=list)
    deliverable_key: str = ""

    # Runtime state
    status: MilestoneStatus = MilestoneStatus.PENDING
    actions_taken: int = 0
    result_summary: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "success_signal": self.success_signal,
            "hint_tools": self.hint_tools,
            "status": self.status.value,
            "actions_taken": self.actions_taken,
            "result_summary": self.result_summary,
            "error": self.error,
        }


# ═══════════════════════════════════════════════════════════════
#  Milestone Plan
# ═══════════════════════════════════════════════════════════════

@dataclass
class MilestonePlan:
    """
    A milestone-based execution plan.

    The planner produces milestones (high-level goals). At runtime
    the executor's micro-loop calls the LLM to pick the next tool
    call for each milestone until the success_signal is observed.
    """
    task_summary: str
    milestones: List[Milestone] = field(default_factory=list)
    final_response: str = ""
    needs_clarification: bool = False
    clarification_prompt: str = ""
    skill_context: str = ""
    skills_used: List[str] = field(default_factory=list)

    # Runtime state
    started_at: float = 0.0
    completed_at: float = 0.0
    confidence: float = 0.0
    source: str = "milestone_planner"

    def get_current_milestone(self) -> Optional[Milestone]:
        """Get the next pending milestone."""
        for m in self.milestones:
            if m.status == MilestoneStatus.PENDING:
                return m
        return None

    def mark_milestone_in_progress(self, milestone_id: int):
        m = self._by_id(milestone_id)
        if m:
            m.status = MilestoneStatus.IN_PROGRESS

    def mark_milestone_complete(self, milestone_id: int, result_summary: str = ""):
        m = self._by_id(milestone_id)
        if m:
            m.status = MilestoneStatus.COMPLETED
            m.result_summary = result_summary

    def mark_milestone_failed(self, milestone_id: int, error: str = ""):
        m = self._by_id(milestone_id)
        if m:
            m.status = MilestoneStatus.FAILED
            m.error = error

    def skip_milestone(self, milestone_id: int, reason: str = ""):
        m = self._by_id(milestone_id)
        if m:
            m.status = MilestoneStatus.SKIPPED
            m.error = reason or "Skipped"

    def is_complete(self) -> bool:
        return all(
            m.status in (MilestoneStatus.COMPLETED, MilestoneStatus.SKIPPED)
            for m in self.milestones
        )

    def has_failed(self) -> bool:
        return any(m.status == MilestoneStatus.FAILED for m in self.milestones)

    def progress_percentage(self) -> float:
        if not self.milestones:
            return 100.0
        done = sum(
            1 for m in self.milestones
            if m.status in (MilestoneStatus.COMPLETED, MilestoneStatus.SKIPPED)
        )
        return (done / len(self.milestones)) * 100

    def to_dict(self) -> dict:
        return {
            "task_summary": self.task_summary,
            "needs_clarification": self.needs_clarification,
            "milestones": [m.to_dict() for m in self.milestones],
            "final_response": self.final_response,
            "skill_context": self.skill_context,
            "skills_used": list(self.skills_used),
            "progress": self.progress_percentage(),
            "is_complete": self.is_complete(),
            "has_failed": self.has_failed(),
        }

    def to_prompt_string(self) -> str:
        lines = [f"Milestone Plan: {self.task_summary}"]
        for m in self.milestones:
            icon = {
                MilestoneStatus.PENDING: "○",
                MilestoneStatus.IN_PROGRESS: "●",
                MilestoneStatus.COMPLETED: "✓",
                MilestoneStatus.FAILED: "✗",
                MilestoneStatus.SKIPPED: "−",
            }.get(m.status, "?")
            lines.append(f"  {icon} M{m.id}: {m.goal}")
            if m.success_signal:
                lines.append(f"       ↳ signal: {m.success_signal}")
        return "\n".join(lines)

    def _by_id(self, milestone_id: int) -> Optional[Milestone]:
        for m in self.milestones:
            if m.id == milestone_id:
                return m
        return None


# ═══════════════════════════════════════════════════════════════
#  Execution Step
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExecutionStep:
    """
    A single step in the execution plan.
    Each step maps to exactly one tool call.
    """
    id: int
    description: str                    # Human-readable action
    tool: str                           # Tool name to call
    args: Dict[str, Any]               # Tool arguments
    success_criteria: str = ""          # How to verify success
    fallback_tool: Optional[str] = None # Alternative if this fails
    fallback_args: Optional[Dict[str, Any]] = None
    depends_on: List[int] = field(default_factory=list)  # Step IDs this depends on
    optional: bool = False              # Can be skipped if it fails
    wait_after: float = 0.0             # Seconds to wait after execution
    
    # Runtime state
    status: StepStatus = StepStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    max_retries: int = 1
    modal_data: Optional[Dict[str, Any]] = None  # Modal context attached during execution

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "tool": self.tool,
            "args": self.args,
            "status": self.status.value,
            "result": self.result,
            "error": self.error
        }

