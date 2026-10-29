"""
Test Multi-Agent — Parallel Milestone Orchestration
=====================================================
Tests for SubAgentManager, RemoteExecutor, and find_parallel_groups.
"""

import asyncio
import sys
import os

# Add backend to path
backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from agent.planner import Milestone, MilestonePlan, MilestoneStatus
from multi_agent import SubAgentConfig, SubAgentResult, SubAgentStatus
from multi_agent.sub_agent_manager import SubAgentManager, find_parallel_groups
from multi_agent.remote_executor import RemoteExecutor


def test_find_parallel_groups_independent():
    """Test that independent milestones are grouped together."""
    print("\n=== Testing find_parallel_groups (independent milestones) ===")

    plan = MilestonePlan(
        task_summary="Compare prices",
        milestones=[
            Milestone(id=1, goal="Find MacBook price", depends_on=[]),
            Milestone(id=2, goal="Find Dell price", depends_on=[]),
            Milestone(id=3, goal="Compare results", depends_on=[1, 2]),
        ]
    )

    groups = find_parallel_groups(plan)

    assert len(groups) == 2, f"Expected 2 groups, got {len(groups)}"
    assert len(groups[0]) == 2, f"Group 0 should have 2 parallel milestones, got {len(groups[0])}"
    assert len(groups[1]) == 1, f"Group 1 should have 1 milestone, got {len(groups[1])}"

    group0_ids = {m.id for m in groups[0]}
    assert group0_ids == {1, 2}, f"Group 0 should be {{1, 2}}, got {group0_ids}"
    assert groups[1][0].id == 3, f"Group 1 should be [3], got {groups[1][0].id}"

    print(f"  ✓ Group 0: {[m.id for m in groups[0]]} (parallel)")
    print(f"  ✓ Group 1: {[m.id for m in groups[1]]} (depends on group 0)")
    print("  Result: PASSED")
    return True


def test_find_parallel_groups_sequential():
    """Test that fully sequential milestones produce single-item groups."""
    print("\n=== Testing find_parallel_groups (sequential milestones) ===")

    plan = MilestonePlan(
        task_summary="Sequential task",
        milestones=[
            Milestone(id=1, goal="Step 1", depends_on=[]),
            Milestone(id=2, goal="Step 2", depends_on=[1]),
            Milestone(id=3, goal="Step 3", depends_on=[2]),
        ]
    )

    groups = find_parallel_groups(plan)

    assert len(groups) == 3, f"Expected 3 groups (no parallelism), got {len(groups)}"
    for i, group in enumerate(groups):
        assert len(group) == 1, f"Group {i} should have 1 milestone"

    print(f"  ✓ 3 sequential groups, each with 1 milestone")
    print("  Result: PASSED")
    return True


def test_find_parallel_groups_all_independent():
    """Test that all-independent milestones form a single group."""
    print("\n=== Testing find_parallel_groups (all independent) ===")

    plan = MilestonePlan(
        task_summary="Parallel task",
        milestones=[
            Milestone(id=1, goal="Task A", depends_on=[]),
            Milestone(id=2, goal="Task B", depends_on=[]),
            Milestone(id=3, goal="Task C", depends_on=[]),
        ]
    )

    groups = find_parallel_groups(plan)

    assert len(groups) == 1, f"Expected 1 group (all parallel), got {len(groups)}"
    assert len(groups[0]) == 3, f"Group 0 should have 3 milestones, got {len(groups[0])}"

    print(f"  ✓ All 3 milestones in a single parallel group")
    print("  Result: PASSED")
    return True


def test_find_parallel_groups_with_completed():
    """Test that already-completed milestones are excluded from groups."""
    print("\n=== Testing find_parallel_groups (with completed milestones) ===")

    plan = MilestonePlan(
        task_summary="Partially done",
        milestones=[
            Milestone(id=1, goal="Done step", depends_on=[],
                      status=MilestoneStatus.COMPLETED),
            Milestone(id=2, goal="Pending step", depends_on=[1]),
            Milestone(id=3, goal="Another pending", depends_on=[1]),
        ]
    )

    groups = find_parallel_groups(plan)

    assert len(groups) == 1, f"Expected 1 group (M1 already done), got {len(groups)}"
    assert len(groups[0]) == 2, f"Group should have M2 and M3, got {len(groups[0])}"

    print(f"  ✓ Completed M1 excluded; M2 and M3 grouped together")
    print("  Result: PASSED")
    return True


def test_remote_executor_placeholder():
    """Test RemoteExecutor in placeholder mode (no execute_fn)."""
    print("\n=== Testing RemoteExecutor (placeholder mode) ===")

    executor = RemoteExecutor(
        agent_id="test_agent_1",
        provider=None,
        tool_declarations=[],
    )

    milestones = [
        Milestone(id=1, goal="Task A", deliverable_key="result_a"),
        Milestone(id=2, goal="Task B", depends_on=[1], deliverable_key="result_b"),
    ]

    result = asyncio.run(executor.execute(milestones=milestones))

    assert result.success, f"Should succeed: {result.error}"
    assert result.milestones_completed == 2, f"Expected 2, got {result.milestones_completed}"
    assert result.milestones_failed == 0
    assert 1 in result.deliverables
    assert 2 in result.deliverables
    assert result.duration_seconds > 0

    print(f"  ✓ Completed {result.milestones_completed} milestones")
    print(f"  ✓ Deliverables: {list(result.deliverables.keys())}")
    print(f"  ✓ Duration: {result.duration_seconds:.3f}s")
    print("  Result: PASSED")
    return True


def test_remote_executor_unmet_dependencies():
    """Test RemoteExecutor skips milestones with unmet dependencies."""
    print("\n=== Testing RemoteExecutor (unmet dependencies) ===")

    executor = RemoteExecutor(
        agent_id="test_agent_2",
        provider=None,
        tool_declarations=[],
    )

    milestones = [
        Milestone(id=3, goal="Depends on M1 and M2", depends_on=[1, 2]),
    ]

    # No parent deliverables for M1 or M2
    result = asyncio.run(executor.execute(milestones=milestones, parent_deliverables={}))

    assert result.milestones_completed == 0
    assert milestones[0].status == MilestoneStatus.SKIPPED

    print(f"  ✓ Milestone skipped due to unmet dependencies")

    # Now with deliverables
    executor2 = RemoteExecutor(
        agent_id="test_agent_3",
        provider=None,
        tool_declarations=[],
    )
    milestones2 = [
        Milestone(id=3, goal="Depends on M1 and M2", depends_on=[1, 2]),
    ]
    result2 = asyncio.run(executor2.execute(
        milestones=milestones2,
        parent_deliverables={1: "MacBook: $2499", 2: "Dell: $1899"}
    ))

    assert result2.milestones_completed == 1
    print(f"  ✓ Milestone completed when dependencies satisfied")
    print("  Result: PASSED")
    return True


def test_remote_executor_cancellation():
    """Test RemoteExecutor stops on cancellation."""
    print("\n=== Testing RemoteExecutor (cancellation) ===")

    executor = RemoteExecutor(
        agent_id="test_cancel",
        provider=None,
        tool_declarations=[],
    )
    executor.cancel()  # Cancel before execution

    milestones = [
        Milestone(id=1, goal="Should not run"),
    ]

    result = asyncio.run(executor.execute(milestones=milestones))

    assert result.status == SubAgentStatus.CANCELLED
    assert result.milestones_completed == 0

    print(f"  ✓ Executor returned CANCELLED status")
    print("  Result: PASSED")
    return True


def test_remote_executor_propagates_await_reply_signal():
    """Test RemoteExecutor does not swallow await-reply suspension control flow."""
    print("\n=== Testing RemoteExecutor (await_reply propagation) ===")

    class AwaitReplySignal(Exception):
        def __init__(self):
            super().__init__()
            self.suspended_milestone_id = 1
            self.await_payload = {"message": "Hello! How can I assist you today?"}
            self.await_data = {"signature": '{"message":"Hello! How can I assist you today?"}'}

    executor = RemoteExecutor(
        agent_id="test_await_signal",
        provider=None,
        tool_declarations=[],
    )

    milestones = [Milestone(id=1, goal="Ask the user a question")]

    async def execute_fn(milestone, deliverables):
        raise AwaitReplySignal()

    try:
        asyncio.run(executor.execute(milestones=milestones, execute_fn=execute_fn))
        raise AssertionError("AwaitReplySignal should have propagated")
    except AwaitReplySignal as exc:
        assert exc.suspended_milestone_id == 1
        assert exc.await_payload["message"] == "Hello! How can I assist you today?"

    print("  ✓ AwaitReplySignal propagated instead of being converted to failure")
    print("  Result: PASSED")
    return True


def test_sub_agent_manager_propagates_parallel_await_reply_signal():
    """Test SubAgentManager re-raises await-reply suspension from parallel tasks."""
    print("\n=== Testing SubAgentManager (parallel await_reply propagation) ===")

    class AwaitReplySignal(Exception):
        def __init__(self, milestone_id: int):
            super().__init__()
            self.suspended_milestone_id = milestone_id
            self.await_payload = {"message": f"Need input for milestone {milestone_id}"}
            self.await_data = {"signature": f'm{milestone_id}'}

    manager = SubAgentManager(
        provider=None,
        tool_declarations=[],
    )

    plan = MilestonePlan(
        task_summary="Need user input",
        milestones=[
            Milestone(id=1, goal="Ask user about option A"),
            Milestone(id=2, goal="Ask user about option B"),
        ]
    )

    async def execute_fn(milestone, deliverables):
        if milestone.id == 1:
            raise AwaitReplySignal(milestone.id)
        return True, f"done {milestone.id}"

    try:
        asyncio.run(manager.dispatch(plan, execute_fn=execute_fn))
        raise AssertionError("AwaitReplySignal should have propagated from parallel dispatch")
    except AwaitReplySignal as exc:
        assert exc.suspended_milestone_id == 1
        assert "Need input" in exc.await_payload["message"]

    print("  ✓ Parallel await_reply suspension propagated out of SubAgentManager")
    print("  Result: PASSED")
    return True


def test_sub_agent_manager_dispatch():
    """Test SubAgentManager dispatch in placeholder mode."""
    print("\n=== Testing SubAgentManager dispatch ===")

    manager = SubAgentManager(
        provider=None,
        tool_declarations=[],
    )

    plan = MilestonePlan(
        task_summary="Compare prices",
        milestones=[
            Milestone(id=1, goal="Find price A", depends_on=[],
                      deliverable_key="price_a"),
            Milestone(id=2, goal="Find price B", depends_on=[],
                      deliverable_key="price_b"),
            Milestone(id=3, goal="Compare", depends_on=[1, 2],
                      deliverable_key="comparison"),
        ]
    )

    deliverables = asyncio.run(manager.dispatch(plan))

    assert 1 in deliverables, "M1 deliverable missing"
    assert 2 in deliverables, "M2 deliverable missing"
    assert 3 in deliverables, "M3 deliverable missing"

    # All milestones should be completed
    for m in plan.milestones:
        assert m.status == MilestoneStatus.COMPLETED, f"M{m.id} should be COMPLETED, got {m.status}"

    print(f"  ✓ All 3 milestones completed")
    print(f"  ✓ Deliverables: {list(deliverables.keys())}")
    print("  Result: PASSED")
    return True


def test_sub_agent_result_types():
    """Test SubAgentResult type properties."""
    print("\n=== Testing SubAgentResult types ===")

    success_result = SubAgentResult(
        agent_id="test",
        status=SubAgentStatus.COMPLETED,
        milestones_completed=2,
        milestones_failed=0,
    )
    assert success_result.success is True

    failed_result = SubAgentResult(
        agent_id="test",
        status=SubAgentStatus.COMPLETED,
        milestones_completed=1,
        milestones_failed=1,
    )
    assert failed_result.success is False

    cancelled_result = SubAgentResult(
        agent_id="test",
        status=SubAgentStatus.CANCELLED,
    )
    assert cancelled_result.success is False

    print("  ✓ success=True when COMPLETED and 0 failures")
    print("  ✓ success=False when COMPLETED but has failures")
    print("  ✓ success=False when CANCELLED")
    print("  Result: PASSED")
    return True


def main():
    print("=" * 60)
    print("  MOONWALK — MULTI-AGENT TESTS")
    print("=" * 60)

    results = []
    results.append(("Parallel groups (independent)", test_find_parallel_groups_independent()))
    results.append(("Parallel groups (sequential)", test_find_parallel_groups_sequential()))
    results.append(("Parallel groups (all independent)", test_find_parallel_groups_all_independent()))
    results.append(("Parallel groups (with completed)", test_find_parallel_groups_with_completed()))
    results.append(("RemoteExecutor (placeholder)", test_remote_executor_placeholder()))
    results.append(("RemoteExecutor (unmet deps)", test_remote_executor_unmet_dependencies()))
    results.append(("RemoteExecutor (cancellation)", test_remote_executor_cancellation()))
    results.append(("RemoteExecutor (await propagation)", test_remote_executor_propagates_await_reply_signal()))
    results.append(("SubAgentManager (await propagation)", test_sub_agent_manager_propagates_parallel_await_reply_signal()))
    results.append(("SubAgentManager dispatch", test_sub_agent_manager_dispatch()))
    results.append(("SubAgentResult types", test_sub_agent_result_types()))

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)

    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"\n  Total: {passed}/{len(results)} test suites passed")
    return passed == len(results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
