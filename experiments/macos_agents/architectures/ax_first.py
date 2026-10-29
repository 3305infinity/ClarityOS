from __future__ import annotations

from ..toolbox import ExperimentToolbox
from ..tools import build_ax_tools, build_low_level_tools
from .base import BaseExperimentAgent


class AXFirstAgent(BaseExperimentAgent):
    agent_name = "ax_first"
    system_prompt = """You are the AX-First macOS experiment agent.
Use Accessibility-grounded tools first:
- activate_app
- get_ui_tree
- semantic_click
- focus_and_type
Only escalate to raw low-level controls after AX lookup fails or times out.
Prefer semantic_click over click_point.
Prefer focus_and_type over type_text when targeting a field.
Use finish_run when the task is complete or blocked."""

    def build_toolbox(self, runtime):
        return ExperimentToolbox(build_ax_tools() + build_low_level_tools())

