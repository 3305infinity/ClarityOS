from __future__ import annotations

from ..toolbox import ExperimentToolbox
from ..tools import build_ax_tools, build_low_level_tools, build_vision_tools
from .base import BaseExperimentAgent


class VisionFirstAgent(BaseExperimentAgent):
    agent_name = "vision_first"
    system_prompt = """You are the Vision-First macOS experiment agent.
Start by observing the screen with vision tools:
- vision_read_screen
- vision_ground_element
Then act with low-level controls such as click_point, run_shortcut, type_text, or press_key.
Use get_ui_tree only as an explicit fallback when visual grounding is uncertain or the app is highly structured.
Do not assume coordinates; ground them first when clicking a target.
Use finish_run when the task is complete or blocked."""

    def build_toolbox(self, runtime):
        return ExperimentToolbox(build_vision_tools() + build_ax_tools() + build_low_level_tools())

