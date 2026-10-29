from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .models import ToolExecutionResult, ToolRuntime


ToolCallable = Callable[[dict, ToolRuntime], Awaitable[ToolExecutionResult]]


@dataclass
class ExperimentTool:
    name: str
    description: str
    parameters: dict
    func: ToolCallable


class ExperimentToolbox:
    def __init__(self, tools: Optional[list[ExperimentTool]] = None):
        self._tools: dict[str, ExperimentTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ExperimentTool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def declarations(self, include: Optional[set[str]] = None) -> list[dict]:
        allowed = include or set(self._tools.keys())
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
            if tool.name in allowed
        ]

    async def execute(self, name: str, args: dict, runtime: ToolRuntime) -> ToolExecutionResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecutionResult(ok=False, message=f"Unknown experiment tool '{name}'")
        try:
            return await tool.func(args or {}, runtime)
        except Exception as exc:  # pragma: no cover - defensive runtime wrapper
            return ToolExecutionResult(ok=False, message=f"Unhandled tool error: {exc}")

    @staticmethod
    def encode_for_model(result: ToolExecutionResult) -> str:
        return json.dumps(result.as_dict(), ensure_ascii=False)

