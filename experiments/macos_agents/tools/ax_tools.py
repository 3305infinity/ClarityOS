from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

from ..models import ToolExecutionResult, ToolRuntime
from ..toolbox import ExperimentTool
from .common import (
    activate_application,
    artifact_path,
    click_point,
    remember_last_text,
    store_artifact,
    tool_failure,
    tool_success,
)


@dataclass
class AXNode:
    role: str
    name: str
    x: int
    y: int
    width: int
    height: int

    @property
    def cx(self) -> int:
        return self.x + self.width // 2

    @property
    def cy(self) -> int:
        return self.y + self.height // 2


_AX_PATTERN = re.compile(
    r'\[(?P<role>[^\]]+)\]\s+"(?P<name>[^"]*)"\s+at\s+(?P<x>\d+),(?P<y>\d+)\s+\(size:\s+(?P<w>\d+)x(?P<h>\d+)\)'
)


def parse_ui_tree(raw: str) -> list[AXNode]:
    nodes: list[AXNode] = []
    for line in str(raw or "").splitlines():
        match = _AX_PATTERN.search(line)
        if not match:
            continue
        nodes.append(
            AXNode(
                role=match.group("role"),
                name=match.group("name"),
                x=int(match.group("x")),
                y=int(match.group("y")),
                width=int(match.group("w")),
                height=int(match.group("h")),
            )
        )
    return nodes


def best_match(nodes: list[AXNode], description: str) -> Optional[AXNode]:
    query = str(description or "").strip().lower()
    if not query:
        return None
    query_tokens = set(query.split())
    scored: list[tuple[int, AXNode]] = []
    for node in nodes:
        name = node.name.lower()
        role = node.role.lower()
        if name == query:
            return node
        score = 0
        if query in name:
            score += 90
        elif name and name in query:
            score += 70
        overlap = query_tokens & set(name.split())
        score += len(overlap) * 15
        if query in role:
            score += 20
        if node.role in {"AXButton", "AXTextField", "AXTextArea", "AXSearchField", "AXLink"}:
            score += 10
        if score > 0:
            scored.append((score, node))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


async def _raw_get_ui_tree(app_name: str = "", search_term: str = "") -> str:
    target_block = 'set targetApp to first process whose frontmost is true'
    if app_name:
        target_block = f'set targetApp to process "{app_name}"'
    search_filter = f'set searchTerm to "{search_term.lower()}"' if search_term else 'set searchTerm to ""'
    script = f'''
    on run
        try
            tell application "System Events"
                {target_block}
                set targetWindow to front window of targetApp
                {search_filter}
                return my dumpUI(targetWindow, "", searchTerm)
            end tell
        on error errMsg
            return "ERROR: " & errMsg
        end try
    end run

    on dumpUI(uiElem, indent, searchTerm)
        set theResult to ""
        try
            tell application "System Events"
                set eRole to role of uiElem as string
                set eName to ""
                try
                    set eName to name of uiElem
                end try
                if eName is missing value then set eName to "unnamed"

                set ePos to {{0, 0}}
                try
                    set ePos to position of uiElem
                end try

                set eSize to {{0, 0}}
                try
                    set eSize to size of uiElem
                end try

                set eLine to "- [" & eRole & "] \\"" & eName & "\\" at " & (item 1 of ePos as string) & "," & (item 2 of ePos as string) & " (size: " & (item 1 of eSize as string) & "x" & (item 2 of eSize as string) & ")" & "\\n"

                set uiChildren to UI elements of uiElem
                set childResults to ""
                set hasMatchingChild to false

                repeat with childElem in uiChildren
                    set childOut to my dumpUI(childElem, indent & "  ", searchTerm)
                    if childOut is not "" then
                        set childResults to childResults & childOut
                        set hasMatchingChild to true
                    end if
                end repeat

                if searchTerm is "" then
                    set theResult to indent & eLine & childResults
                else
                    ignoring case
                        set isMatch to (eName contains searchTerm) or (eRole contains searchTerm)
                    end ignoring
                    if isMatch or hasMatchingChild then
                        set theResult to indent & eLine & childResults
                    end if
                end if
            end tell
        end try
        return theResult
    end dumpUI
    '''
    output = await __import__("experiments.macos_agents.utils", fromlist=["osascript"]).osascript(script, timeout=8.0)
    return output or ("No elements found matching search." if search_term else "Window has no accessible UI elements.")


async def _get_ui_tree(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    app_name = str(args.get("app_name", "") or "").strip()
    search_term = str(args.get("search_term", "") or "").strip()
    if app_name:
        await activate_application(app_name, runtime)
    if runtime.run_mode == "dry":
        sample = '- [AXWindow] "Sample" at 0,0 (size: 800x600)\n  - [AXTextField] "Search" at 40,40 (size: 240x28)'
        path = artifact_path(runtime, "ui_tree", ".txt")
        store_artifact(runtime, "last_ui_tree", path, sample)
        return tool_success("[dry-run] UI tree captured", raw_tree=sample)
    raw = await _raw_get_ui_tree(app_name=app_name, search_term=search_term)
    path = artifact_path(runtime, "ui_tree", ".txt")
    store_artifact(runtime, "last_ui_tree", path, raw)
    if str(raw).startswith("ERROR"):
        return tool_failure(raw, raw_tree=raw)
    return tool_success("UI tree captured", raw_tree=raw)


async def _semantic_click(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    description = str(args.get("description", "") or "").strip()
    app_name = str(args.get("app_name", "") or "").strip()
    if not description:
        return tool_failure("description is required")
    if app_name:
        await activate_application(app_name, runtime)
    raw = await _raw_get_ui_tree(app_name=app_name, search_term="")
    if str(raw).startswith("ERROR"):
        return tool_failure(raw)
    nodes = parse_ui_tree(raw)
    match = best_match(nodes, description)
    if match is None:
        return tool_failure(f"No AX match for '{description}'", description=description)
    click_result = await click_point(match.cx, match.cy, "single", runtime)
    if not click_result.ok:
        return click_result
    return tool_success(
        f"Clicked AX element '{match.name or description}'",
        description=description,
        role=match.role,
        x=match.cx,
        y=match.cy,
    )


async def _focus_and_type(args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
    field_description = str(args.get("field_description", "") or "").strip()
    text = str(args.get("text", "") or "").strip()
    app_name = str(args.get("app_name", "") or "").strip()
    clear_first = bool(args.get("clear_first", False))
    if not field_description:
        return tool_failure("field_description is required")
    if not text:
        return tool_failure("text is required")
    if app_name:
        await activate_application(app_name, runtime)
    raw = await _raw_get_ui_tree(app_name=app_name, search_term="")
    if str(raw).startswith("ERROR"):
        return tool_failure(raw)
    nodes = parse_ui_tree(raw)
    input_nodes = [node for node in nodes if node.role in {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}]
    match = best_match(input_nodes or nodes, field_description)
    if match is None and input_nodes:
        match = sorted(input_nodes, key=lambda node: (node.y, node.x))[0]
    if match is None:
        return tool_failure(f"No AX field match for '{field_description}'")
    click_result = await click_point(match.cx, match.cy, "single", runtime)
    if not click_result.ok:
        return click_result
    await asyncio.sleep(0.15)
    low_level = __import__("experiments.macos_agents.tools.low_level_tools", fromlist=["_run_shortcut", "_press_key", "_type_text"])
    if clear_first:
        shortcut_result = await low_level._run_shortcut({"keys": "command+a"}, runtime)
        if not shortcut_result.ok:
            return shortcut_result
        delete_result = await low_level._press_key({"key": "delete"}, runtime)
        if not delete_result.ok:
            return delete_result
    type_result = await low_level._type_text({"text": text}, runtime)
    if not type_result.ok:
        return type_result
    remember_last_text(runtime, text)
    return tool_success(
        f"Focused '{field_description}' and typed {len(text)} chars",
        role=match.role,
        x=match.cx,
        y=match.cy,
        text=text,
    )


def build_ax_tools() -> list[ExperimentTool]:
    return [
        ExperimentTool(
            name="get_ui_tree",
            description="Dump the Accessibility tree for the active or named app window.",
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {"type": "string"},
                    "search_term": {"type": "string"},
                },
            },
            func=_get_ui_tree,
        ),
        ExperimentTool(
            name="semantic_click",
            description="Click a UI element by matching its Accessibility label or role.",
            parameters={
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "app_name": {"type": "string"},
                },
                "required": ["description"],
            },
            func=_semantic_click,
        ),
        ExperimentTool(
            name="focus_and_type",
            description="Find a text field through the Accessibility tree, focus it, and type into it.",
            parameters={
                "type": "object",
                "properties": {
                    "field_description": {"type": "string"},
                    "text": {"type": "string"},
                    "app_name": {"type": "string"},
                    "clear_first": {"type": "boolean"},
                },
                "required": ["field_description", "text"],
            },
            func=_focus_and_type,
        ),
    ]
