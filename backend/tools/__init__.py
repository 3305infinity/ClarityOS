"""
Moonwalk — Tools Package
=========================
Re-exports the global registry so existing code can do `from tools import registry`.

Importing this package auto-registers all active tool modules.
"""

from tools.registry import registry, ToolDef, ToolRegistry, _osascript
from tools.selector import ToolSelector, ToolCategories, get_tool_selector

# Import all tool modules to trigger their @registry.register decorators
import tools.mac_tools          # noqa: F401  — macOS GUI tools
import tools.file_tools         # noqa: F401  — File I/O tools
import tools.cloud_tools        # noqa: F401  — Cloud-safe tools
import tools.browser_tools      # noqa: F401  — Browser ref tools
import tools.browser_aci        # noqa: F401  — ACI compound browser tools
import tools.gworkspace_tools   # noqa: F401  — Google Workspace tools
import tools.vault_tools        # noqa: F401  — Vault memory tools
import tools.form_tools         # noqa: F401  — Form fill tools
import tools.bolna_tools        # noqa: F401  — Bolna phone voice agents

__all__ = [
    "registry",
    "ToolDef",
    "ToolRegistry",
    "ToolSelector",
    "ToolCategories",
    "get_tool_selector",
]
