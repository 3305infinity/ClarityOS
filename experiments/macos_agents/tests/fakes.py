from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional, Union

from experiments.macos_agents.shared_provider import LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    def __init__(
        self,
        responses: list[Union[LLMResponse, Callable[..., Any]]],
        *,
        name: str = "fake_gemini",
        supports_vision: bool = True,
    ) -> None:
        self._responses = list(responses)
        self._name = name
        self._supports_vision = supports_vision
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    @property
    def supports_tools(self) -> bool:
        return True

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        image_data: Optional[bytes] = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "tools": tools,
                "image_data": image_data,
                "temperature": temperature,
            }
        )
        if not self._responses:
            return LLMResponse(error="No scripted response remaining")
        next_item = self._responses.pop(0)
        if callable(next_item):
            result = next_item(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
                image_data=image_data,
                temperature=temperature,
            )
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return next_item

    async def is_available(self) -> bool:
        return True
