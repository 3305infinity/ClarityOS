"""
Unit tests for research visibility helpers.
"""
import os
import sys
from types import SimpleNamespace

backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from agent.core_v2 import MoonwalkAgentV2
from tools.browser_tools import _build_research_highlight_metadata


def test_build_research_highlight_metadata_includes_overlay_fields():
    snapshot = SimpleNamespace(
        tab_id="15",
        title="Example Title",
        url="https://example.com/article",
        session_id="session-1",
    )

    metadata = _build_research_highlight_metadata(
        snapshot,
        tool_name="extract_structured_data",
        mode="results",
        duration_ms=4500,
        agent_ids=[3, 7, 0, 9],
        snippet="word " * 120,
        item_count=5,
    )

    assert metadata["tab_id"] == "15"
    assert metadata["tool"] == "extract_structured_data"
    assert metadata["mode"] == "results"
    assert metadata["duration"] == "4500"
    assert metadata["source_url"] == "https://example.com/article"
    assert metadata["title"] == "Example Title"
    assert metadata["item_count"] == "5"
    assert metadata["agent_ids"] == [3, 7, 9]
    assert len(metadata["snippet"]) <= 420
    assert metadata["snippet"].endswith("...")


def test_build_research_stream_lines_chunks_long_content():
    agent = MoonwalkAgentV2.__new__(MoonwalkAgentV2)
    content = (
        "London property market trends remain mixed across prime and outer boroughs.\n"
        + ("This sentence carries additional detail about prices, yields, transport, and demand. " * 12)
    )

    lines = agent._build_research_stream_lines(content, max_lines=3)

    assert len(lines) == 3
    assert all(line.strip() for line in lines)
    assert all(len(line) <= 220 for line in lines)
    assert "London property market trends" in lines[0]
