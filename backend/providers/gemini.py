"""
Moonwalk — Gemini Provider (Cloud)
=====================================
Google Gemini — cloud, multimodal, highest quality.
"""

import asyncio
import os
from typing import Optional, AsyncIterator, Any
from functools import partial

from providers.base import LLMProvider, LLMResponse, ToolCall
from providers.errors import ProviderError, ProviderErrorKind, classify_provider_error
from providers.quota import mark_quota_exhausted

print = partial(print, flush=True)


class GeminiProvider(LLMProvider):
    """Google Gemini — cloud, multimodal, highest quality."""

    def __init__(self, api_key: str, model: str = "gemini-3.5-flash"):
        self._model = model
        self._api_key = api_key
        self._client: Any = None
        self._served_model = ""

        if api_key:
            try:
                from google import genai
                from google.genai import types as genai_types
                self._client = genai.Client(api_key=api_key)
                self._genai_types = genai_types
                print(f"[Gemini] Client initialized (model: {model})")
            except ImportError:
                print("[Gemini] google-genai not installed")
            except Exception as e:
                print(f"[Gemini] Init error: {e}")

    @property
    def name(self) -> str:
        return f"gemini ({self._model})"

    @property
    def model(self) -> str:
        """Model id this provider requests."""
        return self._model

    @property
    def served_model(self) -> str:
        """Model id Google reported serving, once known. Falls back to the requested id."""
        return self._served_model or self._model

    def _note_served_model(self, response: Any) -> str:
        """
        Record the model Google actually served.

        Public ids are aliases (`gemini-3.5-flash` is served by `gemini-3.6-flash`),
        so logging only the requested id makes startup logs disagree with API errors.
        """
        served = str(getattr(response, "model_version", "") or "")
        if served and served != self._served_model:
            self._served_model = served
            if served != self._model:
                print(f"[Gemini] {self._model} is served by {served} (alias)")
        return served or self._model

    def _record_failure(self, failure: ProviderError) -> None:
        """Publish daily-quota exhaustion so the router can skip this model family."""
        if failure.kind is ProviderErrorKind.QUOTA_DAILY:
            mark_quota_exhausted(failure, served_model=self._served_model)

    @property
    def supports_vision(self) -> bool:
        return True

    @property
    def supports_tools(self) -> bool:
        return True

    async def is_available(self) -> bool:
        return self._client is not None

    async def generate(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        image_data: Optional[bytes] = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        if not self._client:
            return LLMResponse(error="Gemini client not initialized")

        types = self._genai_types

        # Build tool declarations
        tool_decls = []
        if tools:
            tool_decls = [types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters=t["parameters"],
                )
                for t in tools
            ])]

        # Attach image to last user message if provided
        contents = [dict(m) for m in messages]  # shallow copy
        if image_data and contents and contents[-1].get("role") == "user":
            img_part = types.Part.from_bytes(data=image_data, mime_type="image/png")
            contents[-1] = dict(contents[-1])
            contents[-1]["parts"] = list(contents[-1].get("parts", [])) + [img_part]

        max_api_retries = 3
        last_error = None
        
        for api_attempt in range(max_api_retries + 1):
            try:
                # Conditionally apply thinking budget if requested or on pro/thinking models
                config_kwargs = {
                    "system_instruction": system_prompt,
                    "tools": tool_decls if tool_decls else None,
                    "temperature": temperature,
                }
                if "pro" in self._model or "thinking" in self._model:
                    config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=1024)

                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=self._model,
                        contents=contents,
                        config=types.GenerateContentConfig(**config_kwargs)
                    ),
                    timeout=45.0
                )
                
                # Parse response
                candidate = response.candidates[0] if response.candidates else None
                if not candidate:
                    if api_attempt < max_api_retries:
                        await asyncio.sleep(2.0 * (api_attempt + 1))
                        continue
                    return LLMResponse(text="I'm not sure how to help with that.", provider=self.name)
                    
                content = getattr(candidate, "content", None)
                if not content or not getattr(content, "parts", None):
                    if api_attempt < max_api_retries:
                        await asyncio.sleep(2.0 * (api_attempt + 1))
                        continue
                    return LLMResponse(text="I'm not sure how to help with that.", provider=self.name)

                result = LLMResponse(provider=self.name)
                result.model_version = self._note_served_model(response)
                raw_parts = candidate.content.parts
                result.raw_model_parts = raw_parts

                for part in raw_parts:
                    if part.function_call:
                        fc = part.function_call
                        result.tool_calls.append(ToolCall(
                            name=fc.name,
                            args=dict(fc.args) if fc.args else {}
                        ))
                    elif part.text:
                        result.text = (result.text or "") + part.text

                # If we got only thinking tokens but no text/tool_calls, retry
                if result.text is None and not result.tool_calls:
                    if api_attempt < max_api_retries:
                        print(f"[Gemini] Empty text response (attempt {api_attempt + 1}), retrying...")
                        await asyncio.sleep(2.0 * (api_attempt + 1))
                        continue
                
                return result
                
            except Exception as e:
                last_error = e
                failure = classify_provider_error(e, requested_model=self._model)
                self._record_failure(failure)
                # Quota exhaustion, a bad key or a retired model cannot be fixed by
                # trying again — returning immediately keeps the UI responsive and
                # stops us burning the little quota that may remain.
                if not failure.retryable:
                    print(f"[Gemini] {failure.log_line()} — not retrying")
                    return LLMResponse(error=failure.user_message(), provider=self.name,
                                       provider_error=failure)
                if api_attempt < max_api_retries:
                    delay = failure.retry_after_s or (2.5 * (api_attempt + 1))
                    print(f"[Gemini] {failure.log_line()} (attempt {api_attempt + 1}), "
                          f"retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                return LLMResponse(error=failure.user_message(), provider=self.name,
                                   provider_error=failure)

    async def generate_stream(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        image_data: Optional[bytes] = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[LLMResponse]:
        """Yield LLMResponse 'chunks' over the generate_content_stream API."""
        if not self._client:
            yield LLMResponse(error="Gemini client not initialized", provider=self.name)
            return

        types = self._genai_types
        tool_decls = []
        if tools:
            tool_decls = [types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters=t["parameters"],
                ) for t in tools
            ])]

        contents = []
        for m in messages:
            msg = dict(m)
            if "parts" in msg:
                new_parts = []
                for p in msg["parts"]:
                    # Convert raw dict function_response to SDK Part type
                    if isinstance(p, dict) and "function_response" in p:
                        fr = p["function_response"]
                        new_parts.append(types.Part.from_function_response(
                            name=fr["name"],
                            response=fr["response"]
                        ))
                    else:
                        new_parts.append(p)
                msg["parts"] = new_parts
            contents.append(msg)
            
        if image_data and contents and contents[-1].get("role") == "user":
            img_part = types.Part.from_bytes(data=image_data, mime_type="image/png")
            contents[-1] = dict(contents[-1])
            contents[-1]["parts"] = list(contents[-1].get("parts", [])) + [img_part]

        config_kwargs = {
            "system_instruction": system_prompt,
            "tools": tool_decls if tool_decls else None,
            "temperature": temperature,
        }
        if "pro" in self._model or "thinking" in self._model:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=1024)

        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs)
            )
            
            # Use a wrapper to apply a timeout to each chunk retrieval
            async def iterate_with_timeout():
                # We need to manually drive the async iterator to apply timeouts per-chunk
                stream_iter = stream.__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=60.0)
                        yield chunk
                    except StopAsyncIteration:
                        break
            
            async for chunk in iterate_with_timeout():
                if not chunk.candidates or not chunk.candidates[0].content or not chunk.candidates[0].content.parts:
                    continue
                
                chunk_response = LLMResponse(provider=self.name)
                chunk_response.raw_model_parts = chunk.candidates[0].content.parts
                
                for part in chunk.candidates[0].content.parts:
                    if part.function_call:
                        fc = part.function_call
                        chunk_response.tool_calls.append(ToolCall(
                            name=fc.name,
                            args=dict(fc.args) if fc.args else {}
                        ))
                    elif part.text:
                        chunk_response.text = (chunk_response.text or "") + part.text
                        
                yield chunk_response

        except Exception as e:
            yield LLMResponse(error=f"Gemini streaming error: {e}", provider=self.name)
