"""
Policy engine — one choke point for every tool the agent can run.

Every tool call (browser, desktop, shell, files, messaging, phone calls…) goes through
`registry.execute`; the hub wraps it and asks `PolicyEngine.check()` first.

Per-capability mode (configurable in the website's Settings):
    deny   – the tool never runs
    ask    – the user must approve EVERY call
    allow  – runs, except that *consequential* actions still need approval unless the user
             has explicitly enabled "auto-approve sensitive actions".

Consequential actions = tools that are irreversible / leave the machine (send message,
phone call, shell, write/delete files, upload, quit app) + clicks whose label or stated
reason looks like buy / pay / submit / send / delete (heuristic — it is a safety net, not a
guarantee; see WINDOWS_GUIDE.md).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable

TOOL_CAPABILITY: Dict[str, str] = {}


def _cap(cap: str, names: Iterable[str]) -> None:
    for n in names:
        TOOL_CAPABILITY[n] = cap


_cap("browser", [
    "open_url", "web_search", "play_media", "get_web_information", "extract_structured_data",
    "fetch_web_content", "get_page_summary", "read_page_content", "web_scrape", "fill_form",
    "find_and_act", "browser_assert", "browser_click", "browser_click_match", "browser_click_ref",
    "browser_copy_image", "browser_describe_ref", "browser_fill", "browser_find",
    "browser_list_tabs", "browser_read_page", "browser_read_text", "browser_refresh_refs",
    "browser_scroll", "browser_select_ref", "browser_snapshot", "browser_switch_tab",
    "browser_type_ref", "browser_wait_for"])
_cap("desktop", [
    "open_app", "quit_app", "close_window", "type_text", "press_key", "run_shortcut",
    "click_element", "click_ui", "type_in_field", "mouse_action", "window_manager", "read_screen",
    "get_ui_tree", "get_running_apps", "set_volume", "clipboard_ops", "capture_region_screenshot",
    "copy_image_to_clipboard", "hover_element"])
_cap("shell", ["run_shell", "run_python"])
_cap("files_read", ["read_file", "list_directory"])
_cap("files_write", ["write_file", "replace_in_file"])
_cap("messaging", ["send_imessage", "gmail_send", "gmail_draft"])
_cap("calls", ["bolna_call", "bolna_call_status"])
_cap("google", ["gdocs_create", "gdocs_read", "gdocs_append", "gdocs_insert_image", "gsheets_create",
                "gsheets_read", "gsheets_write", "gsheets_append_rows", "gsheets_formula",
                "gslides_create", "gslides_add_slide", "gdrive_search", "gdrive_upload",
                "gmail_read", "gcal_create_event", "gcal_list_events", "gworkspace_analyze"])
_cap("downloads", ["save_image"])
# Not mapped (always allowed, no side effects outside the agent): send_response, await_reply, wait,
# think, remember_this, recall_memory, forget_this

# Tools that are consequential on every call
ALWAYS_SENSITIVE = {
    "run_shell", "run_python", "write_file", "replace_in_file", "send_imessage", "gmail_send",
    "bolna_call", "gdrive_upload", "gcal_create_event", "quit_app", "save_image", "forget_this",
}

# Tools whose *label / reason* decides (clicks and key presses can submit forms, buy things…)
CONTEXT_SENSITIVE = {
    "browser_click", "browser_click_match", "browser_click_ref", "click_ui", "click_element",
    "find_and_act", "run_shortcut", "press_key", "fill_form", "type_in_field",
}

_RISKY_RE = re.compile(
    r"(?i)\b(buy|purchase|pay(ment)?|checkout|check\s*out|place\s+(your\s+)?order|order\s+now|"
    r"confirm\s+(order|payment|purchase)|submit|send|post|publish|delete|remove|erase|transfer|"
    r"donate|subscribe|unsubscribe|sign\s*out|log\s*out|install|uninstall|format|"
    r"kharid(o|na)?|bhej(o|na)?|payment|bhugtan|hatao|mita(o|na))\b")

_CAPTCHA_RE = re.compile(r"(?i)captcha|recaptcha|i'?m not a robot|verify you are human")


@dataclass
class Decision:
    action: str            # allow | ask | deny
    capability: str = ""
    reason: str = ""
    summary: str = ""


def _flatten(args: Dict[str, Any]) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, default=str)[:600]
    except Exception:
        return str(args)[:600]


class PolicyEngine:
    def __init__(self, get_settings: Callable[[], Dict[str, Any]]):
        self._get = get_settings

    def capability_of(self, tool: str) -> str:
        if tool in TOOL_CAPABILITY:
            return TOOL_CAPABILITY[tool]
        if tool.startswith("browser_"):
            return "browser"
        return ""

    def is_sensitive(self, tool: str, args: Dict[str, Any]) -> str:
        """Return a human reason if this call is consequential, else ''."""
        if tool in ALWAYS_SENSITIVE:
            return f"'{tool}' can change data or leave this computer"
        if tool in CONTEXT_SENSITIVE:
            text = " ".join(str(v) for k, v in args.items() if isinstance(v, (str, int, float)))
            m = _RISKY_RE.search(text)
            if m:
                return f"the action mentions “{m.group(0)}”"
            if tool in ("run_shortcut", "press_key"):
                return ""
        return ""

    def check(self, tool: str, args: Dict[str, Any]) -> Decision:
        settings = self._get()
        cap = self.capability_of(tool)
        summary = f"{tool} {_flatten({k: v for k, v in args.items() if k != 'reasoning'})}"
        if not cap:
            return Decision("allow", "", "", summary)
        mode = settings.get("permissions", {}).get(cap, "ask")
        if mode == "deny":
            return Decision("deny", cap, f"'{cap}' actions are turned off in Settings → Permissions", summary)
        if mode == "ask":
            return Decision("ask", cap, f"'{cap}' actions require approval", summary)
        reason = self.is_sensitive(tool, args)
        if reason and not settings.get("auto_approve_sensitive", False):
            return Decision("ask", cap, reason, summary)
        return Decision("allow", cap, "", summary)


def looks_like_captcha(text: str) -> bool:
    return bool(_CAPTCHA_RE.search(text or ""))
