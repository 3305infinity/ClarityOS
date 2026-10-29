"""
Test Stage 3: Milestone data structures.
"""
import sys
import os

backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from agent.planner import Milestone, MilestonePlan, MilestoneStatus


def test_milestone_creation():
    m = Milestone(
        id=1,
        goal="Search Google for rental market data",
        success_signal="Search results page is loaded with relevant results",
        hint_tools=["web_search", "get_page_summary"],
        deliverable_key="search_results",
    )
    assert m.status == MilestoneStatus.PENDING
    assert m.actions_taken == 0
    d = m.to_dict()
    assert d["goal"] == "Search Google for rental market data"
    assert d["status"] == "pending"


def test_milestone_plan_lifecycle():
    plan = MilestonePlan(
        task_summary="Research UK rental market",
        milestones=[
            Milestone(id=1, goal="Search for rental data", success_signal="Results loaded"),
            Milestone(id=2, goal="Read top 3 sources", success_signal="3 research snippets collected", depends_on=[1]),
            Milestone(id=3, goal="Create Google Doc with findings", success_signal="Doc URL returned", depends_on=[2]),
        ],
        final_response="Research complete!",
    )

    assert not plan.is_complete()
    assert plan.progress_percentage() == 0.0
    assert plan.get_current_milestone().id == 1

    # Complete milestone 1
    plan.mark_milestone_in_progress(1)
    assert plan.milestones[0].status == MilestoneStatus.IN_PROGRESS

    plan.mark_milestone_complete(1, "Found 10 search results")
    assert plan.milestones[0].status == MilestoneStatus.COMPLETED
    assert plan.progress_percentage() > 30

    # Skip milestone 2
    plan.skip_milestone(2, "Not enough sources")
    assert plan.milestones[1].status == MilestoneStatus.SKIPPED

    # Complete milestone 3
    plan.mark_milestone_complete(3, "Doc created")
    assert plan.is_complete()
    assert plan.progress_percentage() == 100.0


def test_milestone_plan_failure():
    plan = MilestonePlan(
        task_summary="Test failure",
        milestones=[
            Milestone(id=1, goal="Do something"),
            Milestone(id=2, goal="Do another thing"),
        ],
    )
    plan.mark_milestone_failed(1, "Network error")
    assert plan.has_failed()
    assert not plan.is_complete()


def test_milestone_plan_serialization():
    plan = MilestonePlan(
        task_summary="Test serialization",
        milestones=[
            Milestone(id=1, goal="Step A", hint_tools=["web_search"]),
            Milestone(id=2, goal="Step B"),
        ],
    )
    d = plan.to_dict()
    assert d["task_summary"] == "Test serialization"
    assert len(d["milestones"]) == 2
    assert d["milestones"][0]["hint_tools"] == ["web_search"]

    prompt = plan.to_prompt_string()
    assert "M1:" in prompt
    assert "Step A" in prompt


def test_milestone_plan_depends_on():
    plan = MilestonePlan(
        task_summary="Dependencies",
        milestones=[
            Milestone(id=1, goal="First"),
            Milestone(id=2, goal="Second", depends_on=[1]),
            Milestone(id=3, goal="Third", depends_on=[1, 2]),
        ],
    )
    assert plan.milestones[2].depends_on == [1, 2]
