"""
ClarityOS — Gnani Evon provider (optional)
==========================================
Evon v3.3 is an open-weight Indic reasoning model. Gnani publishes the weights;
they do **not** publish a hosted inference endpoint, and this module does not
pretend otherwise. It talks to a deployment *you* run and point it at — vLLM,
SGLang or anything else that serves the OpenAI-compatible chat-completions
shape the model card describes.

Configuration (all optional — unset means "Evon is not configured"):

    CLARITYOS_LLM_PROVIDER = gemini | evon     (default: gemini)
    EVON_BASE_URL          = http://<host>:8000/v1    required to use Evon
    EVON_MODEL             = gnani-evon-v3.3          whatever your server serves
    EVON_API_KEY           = sent as `Authorization: Bearer …` when set.
                             Local deployments usually need no key.

Nothing here is Gnani-specific beyond the defaults: the endpoint path, request
schema and auth header are the standard OpenAI ones, which is what the model
card recommends serving. Gemini stays the default and is untouched.
"""

from __future__ import annotations

import json
import os
from functools import partial
from typing import AsyncIterator, Optional

from providers.base import LLMProvider, LLMResponse, ToolCall
from providers.errors import classify_provider_error

print = partial(print, flush=True)

DEFAULT_MODEL = "gnani-evon-v3.3"

# The model card's recommended sampling for this checkpoint.
DEFAULT_TOP_P = 0.95

HTTP_TIMEOUT_S = float(os.environ.get("EVON_TIMEOUT", "120"))


def base_url() -> str:
    """Configured deployment root, or "" when Evon is not set up."""
    return (os.environ.get("EVON_BASE_URL", "") or "").strip().rstrip("/")


def is_configured() -> bool:
    return bool(base_url())


class EvonNotConfigured(RuntimeError):
    """Raised when Evon is selected but no deployment URL was given."""


def _to_openai_messages(messages: list[dict], system_prompt: str) -> list[dict]:
    """Translate ClarityOS's Gemini-shaped history into OpenAI chat messages.

    The rest of the agent speaks Gemini's `{"role", "parts": [...]}` dialect.
    Rather than change every call site, the translation happens here — the same
    place the rest of the provider's shape differences are handled.
    """
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for message in messages or []:
        role = str(message.get("role", "user"))
        if role == "model":
            role = "assistant"          # Gemini's name for it
        if "content" in message:
            out.append({"role": role, "content": message["content"]})
            continue
        chunks: list[str] = []
        for part in message.get("parts", []) or []:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        out.append({"role": role, "content": "\n".join(c for c in chunks if c)})
    return out


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """Gemini function declarations -> OpenAI tool definitions."""
    converted = []
    for tool in tools or []:
        if tool.get("type") == "function" and "function" in tool:
            converted.append(tool)       # already OpenAI-shaped
            continue
        converted.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            },
        })
    return converted


def _tool_calls_from(message: dict) -> list[ToolCall]:
    calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args or "{}")
            except (ValueError, TypeError):
                args = {}
        else:
            args = raw_args or {}
        name = fn.get("name") or ""
        if name:
            calls.append(ToolCall(name=name, args=args))
    return calls


class EvonProvider(LLMProvider):
    """Evon v3.3 through a user-configured OpenAI-compatible deployment."""

    def __init__(self, model: Optional[str] = None,
                 api_key: Optional[str] = None,
                 base: Optional[str] = None):
        self._model = (model or os.environ.get("EVON_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
        self._api_key = (api_key if api_key is not None
                         else os.environ.get("EVON_API_KEY", "")).strip()
        self._base = (base if base is not None else base_url()).rstrip("/")

    # ── identity ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return f"evon:{self._model}"

    @property
    def supports_vision(self) -> bool:
        return False          # Evon v3.3 is a text model

    @property
    def supports_tools(self) -> bool:
        return True           # via the OpenAI tools field, if the server implements it

    # ── plumbing ────────────────────────────────────────────────

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _endpoint(self) -> str:
        if not self._base:
            raise EvonNotConfigured(
                "EVON_BASE_URL is not set. Point it at your Evon deployment, "
                "e.g. http://localhost:8000/v1")
        return f"{self._base}/chat/completions"

    def _body(self, messages, system_prompt, tools, temperature, stream) -> dict:
        body = {
            "model": self._model,
            "messages": _to_openai_messages(messages, system_prompt),
            "temperature": temperature,
            "top_p": DEFAULT_TOP_P,
            "stream": stream,
        }
        converted = _to_openai_tools(tools)
        if converted:
            body["tools"] = converted
        return body

    # ── generation ──────────────────────────────────────────────

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        image_data: Optional[bytes] = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        import httpx

        if image_data:
            # Say so rather than silently answering about nothing.
            print("[Evon] an image was supplied but Evon v3.3 is text-only; ignoring it")

        try:
            body = self._body(messages, system_prompt, tools, temperature, stream=False)
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                resp = await client.post(self._endpoint(), json=body, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        except EvonNotConfigured as e:
            return LLMResponse(error=str(e), provider=self.name)
        except Exception as e:
            failure = classify_provider_error(e, requested_model=self._model)
            return LLMResponse(error=failure.user_message(), provider=self.name,
                               provider_error=failure)

        choices = data.get("choices") or []
        if not choices:
            return LLMResponse(error="Evon returned no choices.", provider=self.name)
        message = choices[0].get("message") or {}
        return LLMResponse(
            text=message.get("content") or None,
            tool_calls=_tool_calls_from(message),
            provider=self.name,
            model_version=str(data.get("model", "") or self._model),
        )

    async def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        image_data: Optional[bytes] = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[LLMResponse]:
        """Token stream over the standard OpenAI SSE format."""
        import httpx

        try:
            body = self._body(messages, system_prompt, tools, temperature, stream=True)
            endpoint = self._endpoint()
        except EvonNotConfigured as e:
            yield LLMResponse(error=str(e), provider=self.name)
            return

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                async with client.stream("POST", endpoint, json=body,
                                         headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return
                        try:
                            data = json.loads(payload)
                        except (ValueError, TypeError):
                            continue
                        for choice in data.get("choices") or []:
                            delta = choice.get("delta") or {}
                            text = delta.get("content")
                            calls = _tool_calls_from(delta)
                            if text or calls:
                                yield LLMResponse(text=text or None, tool_calls=calls,
                                                  provider=self.name)
        except Exception as e:
            failure = classify_provider_error(e, requested_model=self._model)
            yield LLMResponse(error=failure.user_message(), provider=self.name,
                              provider_error=failure)

    # ── availability ────────────────────────────────────────────

    async def is_available(self) -> bool:
        """True only when a deployment is configured and answering.

        An unreachable or unconfigured Evon must report False rather than
        raise, so the router can say so and leave Gemini in place.
        """
        if not self._base:
            return False
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._base}/models", headers=self._headers())
            if resp.status_code < 400:
                return True
            print(f"[Evon] {self._base}/models answered HTTP {resp.status_code}")
            return False
        except Exception as e:
            print(f"[Evon] {self._base} is not reachable: {e.__class__.__name__}")
            return False
