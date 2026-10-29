"""
ClarityOS — LLM Provider Base Types
=====================================
Abstract LLM interface and shared response types.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, AsyncIterator
from functools import partial

from providers.errors import ProviderError, ProviderErrorKind

print = partial(print, flush=True)


# ═══════════════════════════════════════════════════════════════
#  Unified Response Types
# ═══════════════════════════════════════════════════════════════

@dataclass
class ToolCall:
    """A single tool/function call from the LLM."""
    name: str
    args: dict


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""
    text: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    error: Optional[str] = None
    # Raw model response parts — needed for Gemini 3 thought signatures
    raw_model_parts: Optional[list] = None
    # Classified failure (quota / auth / network / …). Set whenever `error` is.
    # Callers branch on this instead of pattern-matching the error string.
    provider_error: Optional["ProviderError"] = None
    # Model id the provider reports actually serving the request. Public ids are
    # aliases, so this can differ from the one we asked for.
    model_version: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def error_kind(self) -> Optional["ProviderErrorKind"]:
        return self.provider_error.kind if self.provider_error else None

    def user_facing_error(self) -> Optional[str]:
        """Actionable message for the UI, or None when the call succeeded."""
        if self.provider_error:
            return self.provider_error.user_message()
        return self.error


# ═══════════════════════════════════════════════════════════════
#  Abstract Provider Interface
# ═══════════════════════════════════════════════════════════════

class LLMProvider(ABC):
    """Abstract interface all LLM providers must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""
        ...

    @property
    @abstractmethod
    def supports_vision(self) -> bool:
        """Whether this provider can handle image inputs."""
        ...

    @property
    @abstractmethod
    def supports_tools(self) -> bool:
        """Whether this provider supports native function calling."""
        ...

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        image_data: Optional[bytes] = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Generate a response from the LLM."""
        ...
        
    async def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        image_data: Optional[bytes] = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[LLMResponse]:
        """Streaming version of generate. Yields LLMResponse chunks."""
        raise NotImplementedError("Streaming not supported by this provider.")
        yield LLMResponse() # for type checking

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if this provider is ready to use."""
        ...
