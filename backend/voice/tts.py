"""
ClarityOS — Streamed TTS via Gnani Timbre v2.5
==============================================
Splits response text into sentences and yields audio chunks in order, so the
first sentence plays while later ones are still being synthesized. Timbre's
SSE endpoint streams within a sentence too, so speech starts before even the
first sentence has finished rendering.

Speech synthesis is Gnani only — there is no second engine and no switch to
one. GNANI_API_KEY is what makes the assistant speak.

Usage:
    from voice.tts import TTSEngine

    tts = TTSEngine()
    async for chunk in tts.stream_synthesize("Hello! How can I help?"):
        # chunk is a TTSChunk with .audio (bytes), .seq (int), .final (bool)
        await ws.send(chunk.to_ws_message())
"""

import asyncio
import base64
import json
import re
import os
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from functools import partial

print = partial(print, flush=True)

# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

# Upper bound on a single synthesis call. The per-engine budget in
# tts_providers.MAX_CHARS is what actually applies; this is the ceiling used
# when an engine does not declare one.
MAX_CHARS_PER_CALL = 4800

# Concurrency limit for parallel sentence synthesis. Per-engine limits in
# tts_providers.MAX_CONCURRENT override it — Gnani accepts one request at a
# time and answers the rest with 429.
MAX_CONCURRENT_SYNTH = 4

# First audio arrives only when the first chunk finishes synthesising, so keep
# that chunk to roughly one sentence. Later chunks batch to limit provider
# calls without affecting when speech starts.
FIRST_CHUNK_CHARS = 140
BATCH_CHUNK_CHARS = 380

# How much of a long answer is spoken before handing off to the screen.
SPEAK_BUDGET_CHARS = 1200


# ═══════════════════════════════════════════════════════════════
#  Data Types
# ═══════════════════════════════════════════════════════════════

@dataclass
class TTSChunk:
    """A single chunk of synthesized audio."""
    audio: bytes        # Raw audio bytes (OGG/OPUS)
    seq: int            # Sequence number (0-based)
    final: bool         # True if this is the last chunk
    text: str           # The sentence that was synthesized

    def to_ws_message(self) -> str:
        """Serialize to a JSON WebSocket message."""
        return json.dumps({
            "type": "tts_chunk",
            "data": base64.b64encode(self.audio).decode("ascii"),
            "seq": self.seq,
            "final": self.final,
        })


# ═══════════════════════════════════════════════════════════════
#  Text Preparation
# ═══════════════════════════════════════════════════════════════

# Markdown patterns to strip before sending to TTS
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"\*(.+?)\*")
_MD_CODE = re.compile(r"`(.+?)`")
_MD_LINK = re.compile(r"\[(.+?)\]\(.+?\)")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_NUMBERED = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)

# Sentence boundary — split on . ! ? followed by space or end
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?।])\s+")


def prepare_for_speech(text: str, display_type: str = "text") -> str:
    """
    Clean and optionally truncate text for spoken output.

    Args:
        text: Raw response text (may contain markdown).
        display_type: "pill" for short responses, "card"/"rich" for long ones.

    Returns:
        Clean spoken text ready for TTS.
    """
    if not text:
        return ""

    # Strip conversation mode markers
    text = text.replace("[CONVERSATION_MODE_ON]", "").replace("[CONVERSATION_MODE_OFF]", "")

    # Strip markdown formatting
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_NUMBERED.sub("", text)

    # Collapse whitespace
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\n", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = text.strip()

    # Long answers used to be cut to the first three sentences, so the user
    # heard an answer that simply stopped — the rest was on screen and never
    # spoken. Speak the whole thing up to a generous budget, and when something
    # really is too long to read out, say so instead of trailing off silently.
    if len(text) > SPEAK_BUDGET_CHARS:
        kept, total = [], 0
        for sentence in _SENTENCE_SPLIT.split(text):
            if total + len(sentence) > SPEAK_BUDGET_CHARS and kept:
                break
            kept.append(sentence)
            total += len(sentence) + 1
        text = " ".join(kept).strip()
        if not text.endswith((".", "!", "?", "।")):
            text += "."
        text += " The rest is on screen."

    return text


def split_sentences(text: str, max_chars: int = MAX_CHARS_PER_CALL) -> list[str]:
    """Split text into chunks for streaming synthesis, first chunk smallest.

    The batching limit used to be the provider's whole per-call budget (4800
    chars), so a five-sentence answer became a single chunk: nothing could be
    heard until the entire reply had been synthesised. Audio only starts when
    the FIRST chunk is ready, so that one is kept to a single sentence and the
    rest are batched — fast first word, without one provider call per sentence.
    """
    if not text:
        return []

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        return [text.strip()] if text.strip() else []

    result: list[str] = []
    current = ""
    # Until the first chunk is emitted, the budget is one short sentence.
    budget = min(FIRST_CHUNK_CHARS, max_chars)

    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > budget:
            result.append(current)
            current = sentence
            budget = min(BATCH_CHUNK_CHARS, max_chars)   # later chunks batch
        else:
            current = f"{current} {sentence}".strip() if current else sentence
        # A long opening sentence should still go out on its own.
        if not result and len(current) >= budget:
            result.append(current)
            current = ""
            budget = min(BATCH_CHUNK_CHARS, max_chars)

    if current:
        result.append(current)

    # If nothing was split (single short sentence), return as-is
    if not result and text.strip():
        result = [text.strip()]

    return result


# ═══════════════════════════════════════════════════════════════
#  TTS Engine
# ═══════════════════════════════════════════════════════════════

class TTSEngine:
    """
    Async streamed TTS via Gnani Timbre v2.5.

    Synthesizes sentences in order and yields audio chunks for streaming
    playback — progressively over SSE when the engine supports it.
    """

    def __init__(self):
        self._enabled = True
        self._announced_failure = False

        from voice import tts_providers
        self.provider_name = "gnani"
        self._alt = tts_providers.build_provider(self.provider_name)
        self._max_chars = tts_providers.MAX_CHARS.get(self.provider_name, MAX_CHARS_PER_CALL)
        self._max_concurrent = tts_providers.MAX_CONCURRENT.get(
            self.provider_name, MAX_CONCURRENT_SYNTH)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    async def _synthesize_one(self, text: str) -> Optional[bytes]:
        """Synthesize one segment with Gnani.

        There is no second engine to try. On failure this says so once and
        returns None: the sentence is skipped, the rest of the answer still
        plays, and `enabled` is deliberately left alone — a bad key or an empty
        balance is a reason to report, not a reason to switch speech off for
        the session.
        """
        try:
            return await self._alt.synthesize(text)
        except Exception as e:
            if not self._announced_failure:
                self._announced_failure = True
                print(f"[TTS] Gnani synthesis failed: {e}")
                print("[TTS] Check GNANI_API_KEY and your Gnani credits.")
            return None








    async def synthesize(self, text: str) -> Optional[bytes]:
        """Synthesize full text as a single audio blob."""
        if not self._enabled or not text:
            return None
        cleaned = prepare_for_speech(text)
        if not cleaned:
            return None
        return await self._synthesize_one(cleaned)

    def _can_stream_audio(self) -> bool:
        """Does the configured engine deliver audio progressively?"""
        return (os.environ.get("CLARITYOS_TTS_STREAM", "1").strip().lower()
                not in ("0", "false", "no")
                and self._alt is not None
                and hasattr(self._alt, "synthesize_stream"))

    async def _stream_from_provider(self, sentences: list[str]) -> AsyncIterator[TTSChunk]:
        """Forward provider chunks as TTSChunks, in order, across all sentences.

        `seq` has to keep rising across the whole answer — the renderer orders
        its playback queue by it — so the provider's per-sentence chunk_index
        is offset rather than used raw. `final` is set once, on the last chunk
        of the last sentence, because that is what stops playback downstream.

        If the stream fails part-way the sentence is retried once over Timbre's
        REST endpoint, which is the same engine by a non-streaming route.
        """
        seq = 0
        last = len(sentences) - 1
        for index, sentence in enumerate(sentences):
            emitted = 0
            try:
                # Hold one chunk back so the last real piece of audio can carry
                # `final`. The stream's own end marker has no audio — emitting
                # it would send the renderer an empty buffer, and skipping it
                # would mean `final` never arrives and playback never ends.
                pending: Optional[TTSChunk] = None
                async for chunk_index, audio, is_final in self._alt.synthesize_stream(sentence):
                    if not audio:
                        continue
                    # synthesize_stream already yields sample-aligned,
                    # independently decodable WAV.
                    if pending is not None:
                        yield pending
                        seq += 1
                    pending = TTSChunk(audio=audio, seq=seq, final=False, text=sentence)
                    emitted += 1
                if pending is not None:
                    pending.final = (index == last)
                    yield pending
                    seq += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[TTS] ⚠ {self.provider_name} streaming failed: {e}")
                if emitted:
                    continue                           # partial audio already spoken
                audio = await self._synthesize_one(sentence)
                if audio:
                    yield TTSChunk(audio=audio, seq=seq,
                                   final=(index == last), text=sentence)
                    seq += 1

    async def stream_synthesize(
        self,
        text: str,
        display_type: str = "text",
    ) -> AsyncIterator[TTSChunk]:
        """
        Stream-synthesize text sentence by sentence.

        Kicks off synthesis for all sentences concurrently (up to
        MAX_CONCURRENT_SYNTH), then yields audio chunks in order
        so the first sentence can play while later ones are still
        being synthesized.

        Args:
            text: Raw response text (may contain markdown).
            display_type: "pill", "card", "rich" — affects truncation.

        Yields:
            TTSChunk objects with audio bytes, sequence number, and final flag.
        """
        if not self._enabled:
            return

        cleaned = prepare_for_speech(text, display_type)
        if not cleaned:
            return

        sentences = split_sentences(cleaned, self._max_chars)
        if not sentences:
            return

        print(f"[TTS] 🔊 Streaming {len(sentences)} sentence(s), "
              f"{len(cleaned)} chars, engine={self.provider_name}")

        # Engines that stream their own audio go down a separate path: waiting
        # for a whole sentence to finish synthesizing before any of it is sent
        # is exactly the delay streaming removes.
        if self._can_stream_audio():
            async for chunk in self._stream_from_provider(sentences):
                yield chunk
            return

        # Launch all synthesis tasks concurrently (with semaphore). The width
        # is the engine's, not a fixed 4: queueing four sentences at Gnani only
        # produced three 429s and a gap in the middle of the answer.
        sem = asyncio.Semaphore(self._max_concurrent)

        async def _synth_with_limit(s: str) -> Optional[bytes]:
            async with sem:
                return await self._synthesize_one(s)

        tasks = [asyncio.create_task(_synth_with_limit(s)) for s in sentences]

        # Yield results in order as they complete
        produced = 0
        for seq, (task, sentence_text) in enumerate(zip(tasks, sentences)):
            try:
                audio = await task
                if audio:
                    produced += 1
                    yield TTSChunk(
                        audio=audio,
                        seq=seq,
                        final=(seq == len(sentences) - 1),
                        text=sentence_text,
                    )
            except asyncio.CancelledError:
                # Cancelled by interrupt — stop yielding
                for remaining_task in tasks[seq + 1:]:
                    remaining_task.cancel()
                return
            except Exception as e:
                print(f"[TTS] ⚠ Sentence {seq} failed: {e}")
                continue

        if produced:
            print(f"[TTS] ✓ Stream complete — {produced}/{len(sentences)} chunk(s) sent")
        else:
            # Silence used to be logged as success, which hid the failure.
            print(f"[TTS] ✗ No audio produced for {len(sentences)} sentence(s) — "
                  f"the reply will be text-only. See the engine errors above.")

    async def speak_ack(self, ack_text: str) -> Optional[bytes]:
        """
        Synthesize a very short acknowledgment (e.g. "On it!").
        Returns raw audio bytes for immediate playback.
        """
        if not self._enabled or not ack_text:
            return None
        return await self._synthesize_one(ack_text)


# ═══════════════════════════════════════════════════════════════
#  Singleton
# ═══════════════════════════════════════════════════════════════

_tts_engine: Optional[TTSEngine] = None


def get_tts_engine() -> TTSEngine:
    """Get or create the singleton TTS engine."""
    global _tts_engine
    if _tts_engine is None:
        _tts_engine = TTSEngine()
    return _tts_engine


def reset_tts_engine() -> None:
    """Drop the singleton so the next call re-reads CLARITYOS_TTS_* / GNANI_* env vars."""
    global _tts_engine
    _tts_engine = None
