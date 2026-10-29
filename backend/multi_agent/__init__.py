"""
Moonwalk — Multi-Agent Orchestration
======================================
Types and protocols for parallel sub-agent execution.

Sub-agents run subsets of milestones in isolation, allowing
independent milestone groups to execute concurrently.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum

from agent.planner import Milestone, MilestonePlan, MilestoneStatus


# ═══════════════════════════════════════════════════════════════
#  Types
# ═══════════════════════════════════════════════════════════════

class SubAgentStatus(str, Enum):
    """Lifecycle status of a sub-agent."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SubAgentConfig:
    """Configuration for spawning a sub-agent."""
    agent_id: str
    milestones: List[Milestone]
    allowed_tools: List[str] = field(default_factory=list)
    parent_deliverables: Dict[int, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    max_actions_per_milestone: int = 50


@dataclass
class SubAgentResult:
    """Result from a sub-agent execution."""
    agent_id: str
    status: SubAgentStatus
    deliverables: Dict[int, str] = field(default_factory=dict)
    milestones_completed: int = 0
    milestones_failed: int = 0
    total_actions: int = 0
    error: str = ""
    duration_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return self.status == SubAgentStatus.COMPLETED and self.milestones_failed == 0


__all__ = [
    "SubAgentStatus",
    "SubAgentConfig",
    "SubAgentResult",
]
