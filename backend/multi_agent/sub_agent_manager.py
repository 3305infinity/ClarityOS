"""
Moonwalk — Sub-Agent Manager
==============================
Orchestrates parallel execution of independent milestone groups.

Given a MilestonePlan, the manager:
1. Builds a dependency graph from milestone `depends_on` fields
2. Identifies groups of milestones that can run in parallel
3. Dispatches each group to RemoteExecutor instances
4. Aggregates results and flows deliverables to dependent milestones

SAFETY: Parallel execution is only used for milestones that don't
interact with the UI/browser.  If any milestone in a group hints at
UI-mutating or browser tools, the group is flattened to sequential
to prevent shared-state race conditions.
"""

import asyncio
import time
import uuid
from typing import List, Dict, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from functools import partial
from collections import defaultdict

print = partial(print, flush=True)

from agent.planner import Milestone, MilestonePlan, MilestoneStatus
from multi_agent import SubAgentConfig, SubAgentResult, SubAgentStatus
from multi_agent.remote_executor import RemoteExecutor, _is_suspend_signal


# Tools that mutate shared state (browser, UI, filesystem) and therefore
# cannot safely run in parallel with other milestones.
_PARALLEL_UNSAFE_TOOLS: frozenset[str] = frozenset({
    # Browser / UI interaction
    "open_url", "open_app", "quit_app", "close_window",
    "browser_click_ref", "browser_type_ref", "browser_select_ref",
    "browser_click_match", "browser_scroll", "browser_read_page",
    "browser_read_text", "browser_refresh_refs", "browser_switch_tab",
    "click_ui", "type_in_field", "type_text", "press_key",
    "mouse_action", "run_shortcut", "click_element",
    "find_and_act", "window_manager",
    # Google Workspace via browser
    "gdocs_create", "gdocs_append", "gsheets_create", "gsheets_write",
    "gslides_create", "gslides_add_slide",
    # Filesystem (potential conflicts on same files)
    "write_file", "replace_in_file",
})


# ═══════════════════════════════════════════════════════════════
#  Parallel Group Analysis
# ═══════════════════════════════════════════════════════════════

def find_parallel_groups(plan: MilestonePlan) -> List[List[Milestone]]:
    """
    Analyze a MilestonePlan and return groups of milestones that can
    run in parallel. Each group is a list of milestones with all
    dependencies satisfied by previous groups.

    Returns:
        List of groups, where each group is a list of Milestones.
        Groups should be executed in order (group 0 first, then group 1, etc.)
        but milestones within each group can run concurrently.
    """
    pending = [
        m for m in plan.milestones
        if m.status in (MilestoneStatus.PENDING, MilestoneStatus.IN_PROGRESS)
    ]
    if not pending:
        return []

    # Build dependency lookup
    completed_ids = {
        m.id for m in plan.milestones
        if m.status == MilestoneStatus.COMPLETED
    }

    groups: List[List[Milestone]] = []
    remaining = list(pending)
    satisfied = set(completed_ids)

    while remaining:
        # Find milestones whose dependencies are all satisfied
        ready = [
            m for m in remaining
            if all(d in satisfied for d in (m.depends_on or []))
        ]

        if not ready:
            # Circular dependency or unsatisfiable — force remaining into one group
            print(f"[SubAgentManager] ⚠ Breaking dependency deadlock for {len(remaining)} milestone(s)")
            groups.append(remaining)
            break

        groups.append(ready)
        for m in ready:
            satisfied.add(m.id)
            remaining.remove(m)

    return groups


# ═══════════════════════════════════════════════════════════════
#  Sub-Agent Manager
# ═══════════════════════════════════════════════════════════════

class SubAgentManager:
    """
    Orchestrates parallel sub-agent execution for a MilestonePlan.

    Usage:
        manager = SubAgentManager(provider=llm, tool_declarations=tools)
        results = await manager.dispatch(plan, deliverables)
    """

    def __init__(
        self,
        provider,
        tool_declarations: list,
        system_prompt: str = "",
        ws_callback: Optional[Callable] = None,
    ):
        self.provider = provider
        self.tool_declarations = tool_declarations
        self.system_prompt = system_prompt
        self.ws_callback = ws_callback
        self._active_agents: Dict[str, RemoteExecutor] = {}

    async def dispatch(
        self,
        plan: MilestonePlan,
        existing_deliverables: Optional[Dict[int, str]] = None,
        execute_fn: Optional[Callable] = None,
        replan_fn: Optional[Callable] = None,
    ) -> Dict[int, str]:
        """
        Execute a MilestonePlan with maximum parallelism.

        Args:
            plan: The MilestonePlan to execute.
            existing_deliverables: Deliverables from already-completed milestones.
            execute_fn: The milestone execution function.
                        Signature: async (milestone, deliverables) -> (success, result_summary)
            replan_fn: Optional async callable invoked after a group failure.
                       Signature: async (plan, failed_milestone_id, failure_reason, deliverables) -> List[Milestone]

        Returns:
            Dict mapping milestone_id -> deliverable string for all completed milestones.
        """
        deliverables = dict(existing_deliverables or {})
        groups = find_parallel_groups(plan)

        if not groups:
            print("[SubAgentManager] No pending milestones to dispatch")
            return deliverables

        total_milestones = sum(len(g) for g in groups)
        total_groups = len(groups)
        max_parallel = max(len(g) for g in groups)
        print(
            f"[SubAgentManager] Dispatching {total_milestones} milestone(s) "
            f"across {total_groups} group(s) (max parallelism: {max_parallel})"
        )

        group_idx = 0
        while group_idx < len(groups):
            group = groups[group_idx]
            group_had_failure = False
            failed_milestone_id = None
            failure_reason = ""

            if len(group) == 1:
                # Sequential: no overhead from sub-agent wrapper
                milestone = group[0]
                result = await self._execute_single(
                    milestone, deliverables, plan, execute_fn
                )
                self._apply_result(result, plan, deliverables)
                if not result.success:
                    group_had_failure = True
                    failed_milestone_id = milestone.id
                    failure_reason = milestone.error or result.error or "Failed"
            else:
                # Parallel: dispatch group concurrently
                print(
                    f"[SubAgentManager] ⚡ Group {group_idx + 1}: "
                    f"executing {len(group)} milestone(s) in parallel"
                )
                results = await self._execute_parallel(
                    group, deliverables, plan, execute_fn
                )
                for i, result in enumerate(results):
                    self._apply_result(result, plan, deliverables)
                    if not result.success and not group_had_failure:
                        group_had_failure = True
                        failed_milestone_id = group[i].id
                        failure_reason = group[i].error or result.error or "Failed"

            # Dynamic replanning after a group failure
            if group_had_failure and replan_fn and failed_milestone_id is not None:
                remaining = [m for m in plan.milestones
                             if m.status == MilestoneStatus.PENDING]
                if remaining:
                    try:
                        revised = await replan_fn(
                            plan=plan,
                            failed_milestone_id=failed_milestone_id,
                            failure_reason=failure_reason,
                            deliverables=deliverables,
                        )
                        if revised:
                            kept = [m for m in plan.milestones
                                    if m.status != MilestoneStatus.PENDING]
                            plan.milestones = kept + revised
                            # Re-analyze groups for the revised plan
                            groups = find_parallel_groups(plan)
                            group_idx = 0
                            continue
                    except Exception as e:
                        print(f"[SubAgentManager] ⚠ Replan error: {e}")

            group_idx += 1

        return deliverables

    async def _execute_single(
        self,
        milestone: Milestone,
        deliverables: Dict[int, str],
        plan: MilestonePlan,
        execute_fn: Optional[Callable],
    ) -> SubAgentResult:
        """Execute a single milestone directly (no sub-agent overhead)."""
        agent_id = f"single_{milestone.id}_{uuid.uuid4().hex[:6]}"
        executor = RemoteExecutor(
            agent_id=agent_id,
            provider=self.provider,
            tool_declarations=self.tool_declarations,
            system_prompt=self.system_prompt,
        )
        self._active_agents[agent_id] = executor

        try:
            result = await executor.execute(
                milestones=[milestone],
                parent_deliverables=deliverables,
                execute_fn=execute_fn,
            )
            return result
        finally:
            self._active_agents.pop(agent_id, None)

    async def _execute_parallel(
        self,
        milestones: List[Milestone],
        deliverables: Dict[int, str],
        plan: MilestonePlan,
        execute_fn: Optional[Callable],
    ) -> List[SubAgentResult]:
        """Execute multiple milestones in parallel via asyncio.gather.
        
        If any milestone hints at UI/browser tools, fall back to sequential
        execution to prevent shared-state race conditions.
        """
        # Safety check: if any milestone touches shared UI/browser state,
        # execute the entire group sequentially to avoid conflicts.
        has_unsafe = any(
            any(
                str(tool).strip() in _PARALLEL_UNSAFE_TOOLS
                for tool in (m.hint_tools or [])
            )
            for m in milestones
        )
        if has_unsafe:
            print(
                f"[SubAgentManager] ⚠ Group has UI/browser tools — "
                f"falling back to sequential for {len(milestones)} milestone(s)"
            )
            results: List[SubAgentResult] = []
            for milestone in milestones:
                result = await self._execute_single(
                    milestone, deliverables, plan, execute_fn
                )
                self._apply_result(result, plan, deliverables)
                results.append(result)
            return results

        tasks = []
        for milestone in milestones:
            agent_id = f"parallel_{milestone.id}_{uuid.uuid4().hex[:6]}"
            executor = RemoteExecutor(
                agent_id=agent_id,
                provider=self.provider,
                tool_declarations=self.tool_declarations,
                system_prompt=self.system_prompt,
            )
            self._active_agents[agent_id] = executor
            tasks.append(
                executor.execute(
                    milestones=[milestone],
                    parent_deliverables=deliverables,
                    execute_fn=execute_fn,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception) and _is_suspend_signal(result):
                raise result

        # Convert exceptions to failed results
        final_results: List[SubAgentResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final_results.append(SubAgentResult(
                    agent_id=f"parallel_{milestones[i].id}",
                    status=SubAgentStatus.FAILED,
                    error=str(result),
                ))
            else:
                final_results.append(result)

        # Clean up agents
        for agent_id in list(self._active_agents.keys()):
            if agent_id.startswith("parallel_"):
                self._active_agents.pop(agent_id, None)

        return final_results

    def _apply_result(
        self,
        result: SubAgentResult,
        plan: MilestonePlan,
        deliverables: Dict[int, str],
    ):
        """Apply a sub-agent result to the plan and deliverables."""
        deliverables.update(result.deliverables)

        if result.success:
            print(
                f"[SubAgentManager] ✓ Agent {result.agent_id}: "
                f"{result.milestones_completed} milestone(s) completed "
                f"in {result.duration_seconds:.1f}s"
            )
        else:
            print(
                f"[SubAgentManager] ✗ Agent {result.agent_id} failed: "
                f"{result.error[:150]}"
            )

    def cancel_all(self):
        """Cancel all active sub-agents."""
        for agent_id, executor in self._active_agents.items():
            executor.cancel()
            print(f"[SubAgentManager] Cancelled agent {agent_id}")
        self._active_agents.clear()
