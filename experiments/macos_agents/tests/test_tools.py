from __future__ import annotations

import asyncio

from experiments.macos_agents.models import ToolRuntime
from experiments.macos_agents.shared_provider import LLMResponse
from experiments.macos_agents.tests.fakes import ScriptedProvider
from experiments.macos_agents.tools import ax_tools, low_level_tools, vision_tools


def test_parse_ui_tree_and_best_match() -> None:
    raw = """
    - [AXWindow] "WhatsApp" at 0,0 (size: 800x600)
      - [AXSearchField] "Search" at 20,20 (size: 240x28)
      - [AXButton] "Kris" at 24,80 (size: 120x24)
    """
    nodes = ax_tools.parse_ui_tree(raw)
    assert len(nodes) == 3
    match = ax_tools.best_match(nodes, "kris")
    assert match is not None
    assert match.name == "Kris"
    assert match.role == "AXButton"


def test_focus_and_type_uses_ax_match_and_updates_runtime(monkeypatch, tmp_path) -> None:
    async def fake_tree(app_name: str = "", search_term: str = "") -> str:
        assert app_name == "WhatsApp"
        return '- [AXWindow] "WhatsApp" at 0,0 (size: 800x600)\n  - [AXSearchField] "Search" at 20,20 (size: 240x28)'

    monkeypatch.setattr(ax_tools, "_raw_get_ui_tree", fake_tree)
    runtime = ToolRuntime(run_mode="dry", artifacts_dir=tmp_path, state={})
    result = asyncio.run(
        ax_tools._focus_and_type(
            {"app_name": "WhatsApp", "field_description": "Search", "text": "Kris"},
            runtime,
        )
    )

    assert result.ok is True
    assert runtime.state["last_typed_text"] == "Kris"
    assert result.payload["text"] == "Kris"


def test_type_text_dry_reuses_last_text(tmp_path) -> None:
    runtime = ToolRuntime(run_mode="dry", artifacts_dir=tmp_path, state={"last_typed_text": "hello again"})
    result = asyncio.run(low_level_tools._type_text({}, runtime))

    assert result.ok is True
    assert result.payload["text"] == "hello again"
    assert "Typed" in result.message


def test_vision_ground_element_parses_provider_response(monkeypatch, tmp_path) -> None:
    screenshot_path = tmp_path / "vision.png"
    screenshot_path.write_bytes(b"png")

    async def fake_capture(runtime: ToolRuntime, prefix: str = "screen"):
        return screenshot_path, "Captured"

    monkeypatch.setattr(vision_tools, "capture_screenshot", fake_capture)
    provider = ScriptedProvider(
        [
            LLMResponse(
                text='{"target":"Search","x":123,"y":45,"confidence":0.91,"rationale":"visible in toolbar"}',
                provider="fake",
            )
        ]
    )
    runtime = ToolRuntime(run_mode="live", artifacts_dir=tmp_path, llm_provider=provider, state={})
    result = asyncio.run(vision_tools._vision_ground_element({"target_description": "Search"}, runtime))

    assert result.ok is True
    assert result.payload["grounding"]["x"] == 123
    assert result.payload["grounding"]["confidence"] == 0.91
    assert provider.calls[0]["image_data"] == b"png"
