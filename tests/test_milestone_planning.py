"""
Test Stage 4: Milestone planning prompts and detection.
"""
import sys
import os
import json

backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from agent.task_planner import TaskPlanner
from agent.planner import MilestonePlan, Milestone, MilestoneStatus
from agent.world_state import WorldState


def test_should_use_milestones_research():
    planner = TaskPlanner()
    assert planner.should_use_milestones("research UK rental market and create a document")
    assert planner.should_use_milestones("investigate the best laptops and write a report")
    assert planner.should_use_milestones("analyze competitor pricing and create a brief")


def test_should_use_milestones_comparison():
    planner = TaskPlanner()
    assert planner.should_use_milestones("compare MacBook Pro vs Dell XPS")
    assert planner.should_use_milestones("what's the difference between React and Vue")


def test_should_use_milestones_simple():
    planner = TaskPlanner()
    assert planner.should_use_milestones("open spotify")
    assert planner.should_use_milestones("set volume to 50")
    assert planner.should_use_milestones("read the file server.py")


def test_parse_milestone_response():
    planner = TaskPlanner()
    raw = json.dumps({
        "task_summary": "Research UK rentals",
        "needs_clarification": False,
        "milestones": [
            {
                "id": 1,
                "goal": "Search for UK rental data",
                "success_signal": "3+ sources found",
                "hint_tools": ["web_search", "read_page_content"],
                "depends_on": [],
                "deliverable_key": "research_data",
            },
            {
                "id": 2,
                "goal": "Create Google Doc",
                "success_signal": "Doc URL returned",
                "hint_tools": ["gdocs_create"],
                "depends_on": [1],
                "deliverable_key": "doc_url",
            },
        ],
        "final_response": "Done!",
    })
    plan = planner._parse_milestone_response(raw, "research UK rentals")
    assert isinstance(plan, MilestonePlan)
    assert len(plan.milestones) == 2
    assert plan.milestones[0].goal == "Search for UK rental data"
    assert plan.milestones[1].depends_on == [1]
    assert plan.milestones[0].deliverable_key == "research_data"


def test_parse_milestone_response_code_fenced():
    planner = TaskPlanner()
    raw = '```json\n{"task_summary":"test","milestones":[{"id":1,"goal":"do it"}],"final_response":"ok"}\n```'
    plan = planner._parse_milestone_response(raw, "test")
    assert len(plan.milestones) == 1
    assert plan.milestones[0].goal == "do it"


def test_parse_milestone_response_clarification():
    planner = TaskPlanner()
    raw = json.dumps({
        "task_summary": "unclear",
        "needs_clarification": True,
        "clarification_prompt": "What do you want?",
        "milestones": [],
        "final_response": "",
    })
    plan = planner._parse_milestone_response(raw, "do it")
    assert plan.needs_clarification
    assert plan.clarification_prompt == "What do you want?"


def test_tool_category_summary():
    planner = TaskPlanner()
    summary = planner._get_tool_category_summary()
    assert "Browser/Web" in summary
    assert "find_and_act" in summary
    assert "Google Workspace" in summary
    assert "gdocs_create" in summary
