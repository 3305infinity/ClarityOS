"""
Moonwalk — Remote Executor
============================
Wraps milestone execution for use as an isolated sub-agent.

Each RemoteExecutor runs a subset of milestones with its own
isolated state, preventing cross-contamination between parallel
sub-agents.
"""

import time
import uuid
from typing import List, Dict, Optional, Callable
from functools import partial

print = partial(print, flush=True)

from agent.planner import Milestone, MilestoneStatus
from multi_agent import SubAgentConfig, SubAgentResult, SubAgentStatus


def _is_suspend_signal(exc: Exception) -> bool:
    """Detect await-reply suspension signals without importing agent internals."""
    return (
        hasattr(exc, "suspended_milestone_id")
        and hasattr(exc, "await_payload")
        and hasattr(exc, "await_data")
    )


class RemoteExecutor:
    """
    Runs milestones as an isolated sub-agent.

    The executor wraps the milestone execution function, tracks results,
    and returns a SubAgentResult with deliverables and status.
    """

    def __init__(
        self,
        agent_id: str,
        provider,
        tool_declarations: list,
        system_prompt: str = "",
        timeout_seconds: float = 120.0,
    ):
        self.agent_id = agent_id
        self.provider = provider
        self.tool_declarations = tool_declarations
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds
        self._cancelled = False
        self._start_time: float = 0.0

    def cancel(self):
        """Signal this executor to stop."""
        self._cancelled = True

    async def execute(
        self,
        milestones: List[Milestone],
        parent_deliverables: Optional[Dict[int, str]] = None,
        execute_fn: Optional[Callable] = None,
    ) -> SubAgentResult:
        """
        Execute a list of milestones sequentially within this sub-agent.

        Args:
            milestones: The milestones to execute (in order).
            parent_deliverables: Deliverables from parent/sibling agents
                                 for dependency resolution.
            execute_fn: The actual milestone execution function.
                        Signature: async (milestone, deliverables) -> (success, result_summary)
                        If not provided, milestones are marked as completed
                        with a placeholder result (useful for testing).

        Returns:
            SubAgentResult with deliverables and status.
        """
        self._start_time = time.time()
        deliverables = dict(parent_deliverables or {})
        completed = 0
        failed = 0
        total_actions = 0

        print(
            f"[RemoteExecutor:{self.agent_id}] Starting "
            f"{len(milestones)} milestone(s)"
        )

        for milestone in milestones:
            if self._cancelled:
                print(f"[RemoteExecutor:{self.agent_id}] Cancelled")
                return SubAgentResult(
                    agent_id=self.agent_id,
                    status=SubAgentStatus.CANCELLED,
                    deliverables=deliverables,
                    milestones_completed=completed,
                    milestones_failed=failed,
                    total_actions=total_actions,
                    duration_seconds=time.time() - self._start_time,
                )

            # Check timeout
            elapsed = time.time() - self._start_time
            if elapsed > self.timeout_seconds:
                print(
                    f"[RemoteExecutor:{self.agent_id}] Timeout after "
                    f"{elapsed:.1f}s"
                )
                return SubAgentResult(
                    agent_id=self.agent_id,
                    status=SubAgentStatus.FAILED,
                    deliverables=deliverables,
                    milestones_completed=completed,
                    milestones_failed=failed,
                    total_actions=total_actions,
                    error=f"Timeout after {elapsed:.1f}s",
                    duration_seconds=elapsed,
                )

            # Check dependencies
            unmet = [
                d for d in (milestone.depends_on or [])
                if d not in deliverables
            ]
            if unmet:
                print(
                    f"[RemoteExecutor:{self.agent_id}] Skipping M{milestone.id}: "
                    f"unmet dependencies {unmet}"
                )
                milestone.status = MilestoneStatus.SKIPPED
                milestone.error = f"Dependencies {unmet} not available"
                continue

            # Execute the milestone
            milestone.status = MilestoneStatus.IN_PROGRESS
            try:
                if execute_fn:
                    success, result_summary = await execute_fn(
                        milestone, deliverables
                    )
                else:
                    # Test/placeholder mode
                    success = True
                    result_summary = f"[placeholder] Completed: {milestone.goal}"

                if success:
                    milestone.status = MilestoneStatus.COMPLETED
                    milestone.result_summary = result_summary or ""
                    if milestone.deliverable_key:
                        deliverables[milestone.id] = result_summary or ""
                    completed += 1
                    total_actions += milestone.actions_taken
                    print(
                        f"[RemoteExecutor:{self.agent_id}] ✓ M{milestone.id}: "
                        f"{milestone.goal[:80]}"
                    )
                else:
                    milestone.status = MilestoneStatus.FAILED
                    milestone.error = result_summary or "Failed"
                    failed += 1
                    total_actions += milestone.actions_taken
                    print(
                        f"[RemoteExecutor:{self.agent_id}] ✗ M{milestone.id}: "
                        f"{result_summary[:100]}"
                    )

            except Exception as e:
                if _is_suspend_signal(e):
                    raise
                milestone.status = MilestoneStatus.FAILED
                milestone.error = str(e)
                failed += 1
                print(
                    f"[RemoteExecutor:{self.agent_id}] ✗ M{milestone.id} "
                    f"exception: {e}"
                )

        duration = time.time() - self._start_time
        status = (
            SubAgentStatus.COMPLETED if failed == 0
            else SubAgentStatus.FAILED
        )

        return SubAgentResult(
            agent_id=self.agent_id,
            status=status,
            deliverables=deliverables,
            milestones_completed=completed,
            milestones_failed=failed,
            total_actions=total_actions,
            error="" if failed == 0 else f"{failed} milestone(s) failed",
            duration_seconds=duration,
        )
