"""
Test Replanning — Dynamic Milestone Replanning
================================================
Tests for TaskPlanner.replan_remaining() and _parse_replan_response().
"""

import asyncio
import sys
import os
import json

# Add backend to path
backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from agent.planner import Milestone, MilestonePlan, MilestoneStatus
from agent.task_planner import TaskPlanner


def test_parse_replan_response_valid():
    """Test parsing a valid replan JSON response."""
    print("\n=== Testing _parse_replan_response (valid JSON) ===")
    planner = TaskPlanner()

    response = json.dumps({
        "milestones": [
            {
                "id": 4,
                "goal": "Try alternative approach for search",
                "success_signal": "Results found via alternative query",
                "hint_tools": ["get_web_information"],
                "depends_on": [],
                "deliverable_key": "alt_search_results"
            },
            {
                "id": 5,
                "goal": "Create document with available data",
                "success_signal": "Google Doc created with content",
                "hint_tools": ["gdocs_create"],
                "depends_on": [4],
                "deliverable_key": "document_url"
            }
        ],
        "recovery_strategy": "Use alternative search query since original failed"
    })

    result = planner._parse_replan_response(response, start_id=4)
    assert result is not None, "Should parse valid response"
    assert len(result) == 2, f"Expected 2 milestones, got {len(result)}"
    assert result[0].id == 4
    assert result[0].goal == "Try alternative approach for search"
    assert result[1].depends_on == [4]
    print("  ✓ Parsed 2 milestones with correct structure")
    print("  Result: PASSED")
    return True


def test_parse_replan_response_with_markdown():
    """Test parsing response wrapped in markdown code block."""
    print("\n=== Testing _parse_replan_response (markdown wrapper) ===")
    planner = TaskPlanner()

    response = '```json\n{"milestones": [{"id": 3, "goal": "Retry search", "success_signal": "Results found"}], "recovery_strategy": "retry"}\n```'

    result = planner._parse_replan_response(response, start_id=3)
    assert result is not None, "Should handle markdown-wrapped JSON"
    assert len(result) == 1
    assert result[0].id == 3
    print("  ✓ Correctly stripped markdown wrapper and parsed JSON")
    print("  Result: PASSED")
    return True


def test_parse_replan_response_invalid():
    """Test that invalid JSON returns None."""
    print("\n=== Testing _parse_replan_response (invalid JSON) ===")
    planner = TaskPlanner()

    result = planner._parse_replan_response("not json at all", start_id=1)
    assert result is None, "Should return None for invalid JSON"
    print("  ✓ Returns None for invalid JSON")

    result = planner._parse_replan_response('{"milestones": "not a list"}', start_id=1)
    assert result is None, "Should return None when milestones is not a list"
    print("  ✓ Returns None when milestones is not a list")

    result = planner._parse_replan_response('{"milestones": []}', start_id=1)
    assert result is None, "Should return None for empty milestones"
    print("  ✓ Returns None for empty milestones list")

    print("  Result: PASSED")
    return True


def test_replan_fallback_without_provider():
    """Test that replan_remaining() falls back to retry milestone without LLM."""
    print("\n=== Testing replan_remaining (fallback without provider) ===")
    planner = TaskPlanner(provider=None)

    plan = MilestonePlan(
        task_summary="Research and create document",
        milestones=[
            Milestone(id=1, goal="Search for data", success_signal="Data found",
                      status=MilestoneStatus.COMPLETED, result_summary="Found 3 sources"),
            Milestone(id=2, goal="Process results", success_signal="Results processed",
                      status=MilestoneStatus.FAILED, error="Processing failed"),
            Milestone(id=3, goal="Create document", success_signal="Doc created",
                      depends_on=[2], deliverable_key="doc_url"),
        ]
    )

    result = asyncio.run(planner.replan_remaining(
        plan=plan,
        failed_milestone_id=2,
        failure_reason="Processing failed due to timeout",
        deliverables={1: "Found 3 data sources"},
    ))

    assert len(result) == 1, f"Fallback should produce 1 retry milestone, got {len(result)}"
    assert "Retry" in result[0].goal, f"Should be a retry milestone: {result[0].goal}"
    assert result[0].id == 4, f"Next ID should be 4, got {result[0].id}"
    print(f"  ✓ Fallback produced retry milestone: '{result[0].goal}'")
    print(f"  ✓ Correct next_id: {result[0].id}")
    print("  Result: PASSED")
    return True


def test_replan_preserves_completed_deliverables():
    """Test that replanning does not affect completed milestone deliverables."""
    print("\n=== Testing replan preserves deliverables ===")
    planner = TaskPlanner(provider=None)

    deliverables = {1: "MacBook Pro: $2499", 2: "Dell XPS: $1899"}

    plan = MilestonePlan(
        task_summary="Compare laptop prices",
        milestones=[
            Milestone(id=1, goal="Find MacBook price", status=MilestoneStatus.COMPLETED,
                      result_summary="MacBook Pro: $2499", deliverable_key="macbook_price"),
            Milestone(id=2, goal="Find Dell price", status=MilestoneStatus.COMPLETED,
                      result_summary="Dell XPS: $1899", deliverable_key="dell_price"),
            Milestone(id=3, goal="Compare and present", status=MilestoneStatus.FAILED,
                      error="Comparison formatting failed"),
        ]
    )

    result = asyncio.run(planner.replan_remaining(
        plan=plan,
        failed_milestone_id=3,
        failure_reason="Comparison formatting failed",
        deliverables=deliverables,
    ))

    # Deliverables from M1 and M2 should still be in the dict
    assert deliverables[1] == "MacBook Pro: $2499"
    assert deliverables[2] == "Dell XPS: $1899"
    print("  ✓ Existing deliverables preserved after replanning")

    # The retry milestone should have the correct next ID
    assert result[0].id == 4
    print(f"  ✓ Retry milestone has correct ID: {result[0].id}")
    print("  Result: PASSED")
    return True


def main():
    print("=" * 60)
    print("  MOONWALK — REPLANNING TESTS")
    print("=" * 60)

    results = []
    results.append(("Parse valid JSON", test_parse_replan_response_valid()))
    results.append(("Parse markdown wrapper", test_parse_replan_response_with_markdown()))
    results.append(("Parse invalid JSON", test_parse_replan_response_invalid()))
    results.append(("Fallback without provider", test_replan_fallback_without_provider()))
    results.append(("Preserve deliverables", test_replan_preserves_completed_deliverables()))

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
