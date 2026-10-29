"""
ClarityOS — Streamed TTS via Google Cloud Text-to-Speech (Neural2)
==================================================================
Splits response text into sentences, synthesizes each sentence
concurrently, and yields audio chunks in order so the first sentence
plays while later ones are still synthesizing.

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

# Default Neural2 voice — natural, mid-cost ($16/1M chars)
DEFAULT_VOICE = os.environ.get("CLARITYOS_TTS_VOICE", "en-US-Neural2-J")
DEFAULT_LANGUAGE = os.environ.get("CLARITYOS_TTS_LANGUAGE", "en-US")
DEFAULT_SPEAKING_RATE = float(os.environ.get("CLARITYOS_TTS_SPEED", "1.05"))

# Audio encoding: OGG_OPUS is compact and plays natively in Chromium
AUDIO_ENCODING = "OGG_OPUS"

# Max characters per synthesis call (Google TTS limit is 5000)
MAX_CHARS_PER_CALL = 4800

# Concurrency limit for parallel sentence synthesis
MAX_CONCURRENT_SYNTH = 4


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

    # For long responses (card/rich), truncate to ~3 sentences for speech
    if display_type in ("card", "rich") and len(text) > 300:
        sentences = _SENTENCE_SPLIT.split(text)
        text = " ".join(sentences[:3])
        if not text.endswith((".", "!", "?", "।")):
            text += "."

    return text


def split_sentences(text: str, max_chars: int = MAX_CHARS_PER_CALL) -> list[str]:
    """Split text into sentence-sized chunks for streaming synthesis."""
    if not text:
        return []

    sentences = _SENTENCE_SPLIT.split(text)
    result = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # If adding this sentence would exceed the limit, flush current
        if current and len(current) + len(sentence) + 1 > max_chars:
            result.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence

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
    Async streamed TTS via Google Cloud Text-to-Speech.

    Lazily initializes the Google TTS client on first use.
    Synthesizes sentences concurrently and yields audio chunks
    in order for streaming playback.
    """

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        language: str = DEFAULT_LANGUAGE,
        speaking_rate: float = DEFAULT_SPEAKING_RATE,
    ):
        self.voice_name = voice
        self.language_code = language
        self.speaking_rate = speaking_rate
        self._client = None
        self._enabled = True
        # Offline backstop, built on first need (see _get_local).
        self._local = None
        self._local_tried = False
        self._google_ok = True
        self._announced_google_down = False
        self._announced_local = False

        # Pluggable engine: google (default) | gnani | sarvam  — see voice/tts_providers.py
        from voice import tts_providers
        self.provider_name = os.environ.get("CLARITYOS_TTS_PROVIDER", "google").strip().lower()
        self._alt = tts_providers.build_provider(self.provider_name)
        self._max_chars = tts_providers.MAX_CHARS.get(self.provider_name, MAX_CHARS_PER_CALL)
        if self.provider_name not in ("google", "") and self._alt is None:
            print(f"[TTS] Unknown CLARITYOS_TTS_PROVIDER '{self.provider_name}' — using google")
            self.provider_name = "google"
        # If the alternative engine fails (bad key, offline, quota) fall back to Google once.
        self._fallback_google = os.environ.get("CLARITYOS_TTS_FALLBACK", "google").strip().lower() == "google"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    def _get_client(self):
        """Lazily create the Google TTS client."""
        if self._client is None:
            try:
                from google.cloud import texttospeech
                self._client = texttospeech.TextToSpeechAsyncClient()
                print("[TTS] ✓ Google Cloud TTS client initialized")
            except ImportError:
                self._report_google_unavailable(
                    "google-cloud-texttospeech is not installed")
                return None
            except Exception as e:
                # Almost always missing Application Default Credentials. Speech
                # only: planning and desktop actions do not use Google Cloud auth.
                detail = str(e)
                if "default credentials" in detail.lower():
                    self._report_google_unavailable(
                        "no Google Cloud credentials (set GOOGLE_APPLICATION_CREDENTIALS "
                        "in backend/.env, or CLARITYOS_TTS_PROVIDER=gnani|sarvam with that "
                        "provider's key)")
                else:
                    self._report_google_unavailable(detail[:200])
                return None
        return self._client

    def _report_google_unavailable(self, why: str) -> None:
        """
        Log once and stand down from Google — without disabling speech.

        This used to set `_enabled = False`, which made `stream_synthesize`
        return immediately and produce zero chunks, so nothing ever reached the
        renderer. The offline voice now covers this case instead.
        """
        self._google_ok = False
        if not self._announced_google_down:
            self._announced_google_down = True
            print(f"[TTS] ⚠ Google Cloud TTS unavailable — {why}. "
                  f"Falling back to the built-in voice; Gemini planning is unaffected.")

    async def _synthesize_one(self, text: str) -> Optional[bytes]:
        """
        Synthesize one segment: configured engine → Google → local Windows voice.

        The local engine is the backstop that keeps the assistant audible when no
        cloud credentials are configured. Returning None here means every engine
        failed, and the reason has already been logged.
        """
        if self._alt is not None:
            try:
                return await self._alt.synthesize(text)
            except Exception as e:
                print(f"[TTS] ⚠ {self.provider_name} synthesis failed: {e}")
                if not self._fallback_google:
                    return await self._synthesize_local(text)
                print("[TTS] ↪ falling back to Google TTS for this sentence")

        audio = await self._synthesize_google(text)
        if audio:
            return audio
        return await self._synthesize_local(text)

    def _get_local(self):
        """Lazily build the offline Windows synthesizer (None off Windows)."""
        if self._local is None and not self._local_tried:
            self._local_tried = True
            from voice import tts_providers
            self._local = tts_providers.build_local_provider()
            if self._local is None:
                print("[TTS] ⚠ No offline voice available on this platform")
        return self._local

    async def _synthesize_local(self, text: str) -> Optional[bytes]:
        """Last resort: the built-in OS voice. Needs no credentials."""
        provider = self._get_local()
        if provider is None:
            self._note_all_engines_failed()
            return None
        try:
            audio = await provider.synthesize(text)
        except Exception as e:
            print(f"[TTS] ⚠ Offline voice failed: {e}")
            self._note_all_engines_failed()
            return None
        if audio and not self._announced_local:
            self._announced_local = True
            print("[TTS] 🔈 Using the built-in Windows voice "
                  "(set GOOGLE_APPLICATION_CREDENTIALS, GNANI_API_KEY or "
                  "SARVAM_API_KEY for a higher-quality voice)")
        return audio

    def _note_all_engines_failed(self) -> None:
        """Disable only when nothing at all can speak, and say so once."""
        if self._enabled:
            self._enabled = False
            print("[TTS] ⚠ Every speech engine failed — replies will be text-only")

    async def _synthesize_google(self, text: str) -> Optional[bytes]:
        client = self._get_client()
        if not client:
            return None

        try:
            from google.cloud import texttospeech

            request = texttospeech.SynthesizeSpeechRequest(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(
                    language_code=self.language_code,
                    name=self.voice_name,
                ),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.OGG_OPUS,
                    speaking_rate=self.speaking_rate,
                    pitch=0.0,
                ),
            )

            response = await client.synthesize_speech(request=request)
            return response.audio_content

        except Exception as e:
            print(f"[TTS] ⚠ Synthesis error: {e}")
            return None

    async def synthesize(self, text: str) -> Optional[bytes]:
        """Synthesize full text as a single audio blob."""
        if not self._enabled or not text:
            return None
        cleaned = prepare_for_speech(text)
        if not cleaned:
            return None
        return await self._synthesize_one(cleaned)

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

        # Launch all synthesis tasks concurrently (with semaphore)
        sem = asyncio.Semaphore(MAX_CONCURRENT_SYNTH)

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
    """Drop the singleton so the next call re-reads CLARITYOS_TTS_* / GNANI_* / SARVAM_* env vars."""
    global _tts_engine
    _tts_engine = None
