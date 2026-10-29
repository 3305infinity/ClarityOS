"""
Moonwalk — Shared Agent Constants
===================================
Canonical tool-category sets used across the agent subsystem.
Import from here — never re-declare locally.
"""

# ═══════════════════════════════════════════════════════════════
#  Tool Categories
# ═══════════════════════════════════════════════════════════════

# Tools that mutate visible UI state (trigger screenshot recovery on failure,
# trigger post-action visual verification, etc.).
UI_MUTATING_TOOLS: frozenset[str] = frozenset({
    # OS-level interaction
    "click_ui", "type_in_field", "type_text", "press_key",
    "run_shortcut", "click_element", "hover_element", "mouse_action",
    # Browser ref-based interaction
    "browser_click_ref", "browser_type_ref", "browser_select_ref",
    "browser_click_match", "find_and_act",
    # App lifecycle
    "open_app", "open_url", "quit_app", "close_window",
    # Google Workspace mutating actions
    "gdocs_create", "gdocs_write", "gsheets_write", "gcal_create_event",
})

# Read-only / observation tools — safe to run without side-effects.
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file", "list_directory", "get_web_information", "web_scrape",
    "web_search", "browser_read_page", "browser_read_text",
    "read_page_content", "extract_structured_data", "get_page_summary",
    "browser_snapshot", "browser_find", "get_ui_tree", "read_screen",
    "gdocs_read", "gsheets_read", "gdrive_search",
    "gmail_read", "gcal_list_events", "gworkspace_analyze",
})

# Browser-related tools that require an active browser bridge.
BROWSER_TOOLS: frozenset[str] = frozenset({
    "browser_click_ref", "browser_type_ref", "browser_select_ref",
    "browser_click_match", "browser_read_page", "browser_read_text",
    "browser_scroll", "browser_snapshot", "browser_find",
    "browser_list_tabs", "browser_switch_tab",
    "open_url", "find_and_act",
})

# Tools that make trivial forward progress — milestones should not be
# considered "done" if the only calls were these.
TRIVIAL_PROGRESS_TOOLS: frozenset[str] = frozenset({
    "open_url",
    "browser_scroll",
    "web_search",
})

# Tools whose results carry low observational signal for verification.
LOW_SIGNAL_ACTION_TOOLS: frozenset[str] = frozenset({
    "press_key",
    "run_shortcut",
    "mouse_action",
    "browser_scroll",
    "web_search",
})
