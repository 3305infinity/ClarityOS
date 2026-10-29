"""
ClarityOS — Pluggable Speech-to-Text
====================================
One interface, several engines. Pick one with env vars (see WINDOWS_GUIDE.md):

    CLARITYOS_STT_PROVIDER = google | gnani | sarvam      (default: google)
    CLARITYOS_STT_FALLBACK = google | gnani | sarvam | "" (default: google when the
                            primary is not google, so a network/API hiccup never
                            leaves the mic dead)
    CLARITYOS_STT_LANGUAGE = BCP-47 code, e.g. en-IN, hi-IN, ta-IN

Engines
  google  – free Google Web Speech via the `SpeechRecognition` package (original behaviour)
  gnani   – Gnani Vachana "Prisma v2.5"  POST https://api.vachana.ai/stt/v3
              key: GNANI_API_KEY      (sent as X-API-Key-ID)
              en-IN accepts English+Hindi mixed speech and answers in Latin script;
              hi-IN answers in Devanagari.
  sarvam  – Sarvam Saaras/Saarika        POST https://api.sarvam.ai/speech-to-text
              key: SARVAM_API_KEY     (sent as api-subscription-key)

Contract used by the servers:
    text = await provider.transcribe(pcm16_mono_16k_bytes)
    raises NoSpeechError    -> audio had no intelligible speech (user stayed quiet)
    raises STTRequestError  -> network / quota / auth problem
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import wave
from typing import Optional

SAMPLE_RATE = 16000
HTTP_TIMEOUT_S = float(os.environ.get("CLARITYOS_STT_TIMEOUT", "20"))


# ═══════════════════════════════════════════════════════════════
#  Errors
# ═══════════════════════════════════════════════════════════════

class STTError(Exception):
    """Base class for speech-to-text failures."""


class NoSpeechError(STTError):
    """The audio was processed fine but contained no recognisable speech."""


class STTRequestError(STTError):
    """The engine could not be reached / rejected the request."""


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


def _error_message(resp) -> str:
    """Best-effort extraction of an API error message from an httpx response."""
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if isinstance(data, dict):
            return str(data.get("message") or data.get("detail") or data)[:300]
    except Exception:
        pass
    return (resp.text or "")[:300]


def _raise_for_status(provider: str, resp) -> None:
    if resp.status_code < 400:
        return
    msg = _error_message(resp)
    raise STTRequestError(f"{provider} STT HTTP {resp.status_code}: {msg}")


# ═══════════════════════════════════════════════════════════════
#  Provider base
# ═══════════════════════════════════════════════════════════════

class STTProvider:
    name = "base"

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════
#  Google Web Speech (original behaviour, now off the event loop)
# ═══════════════════════════════════════════════════════════════

class GoogleSTT(STTProvider):
    name = "google"

    def __init__(self, language: Optional[str] = None):
        self.language = language or os.environ.get("CLARITYOS_STT_LANGUAGE", "en-US")
        self._recognizer = None

    def _blocking_transcribe(self, wav_bytes: bytes) -> str:
        import speech_recognition as sr  # imported lazily: optional dependency

        if self._recognizer is None:
            self._recognizer = sr.Recognizer()
        try:
            with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
                audio = self._recognizer.record(source)
            return self._recognizer.recognize_google(audio, language=self.language)
        except sr.UnknownValueError as e:
            raise NoSpeechError("Google STT could not understand audio") from e
        except sr.RequestError as e:
            raise STTRequestError(f"Google STT request failed: {e}") from e

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        wav = pcm_to_wav(pcm, sample_rate)
        return await asyncio.to_thread(self._blocking_transcribe, wav)


# ═══════════════════════════════════════════════════════════════
#  Gnani Vachana (Prisma v2.5)
# ═══════════════════════════════════════════════════════════════

class GnaniSTT(STTProvider):
    """
    REST: POST /stt/v3 (multipart). Clips up to 60 s (ideal <= 30 s), which is
    plenty for voice commands. Audio is auto-converted to 16 kHz mono server-side.
    """
    name = "gnani"
    URL = "https://api.vachana.ai/stt/v3"

    def __init__(self, api_key: Optional[str] = None, language: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GNANI_API_KEY", "")
        self.language = language or os.environ.get("CLARITYOS_STT_LANGUAGE", "en-IN")
        # "transcribe" = ITN on: "five thousand rupees" -> "₹5,000". Better for commands.
        # "verbatim"   = raw spoken form.
        self.format = os.environ.get("GNANI_STT_FORMAT", "transcribe")
        # Optional word boosting: comma-separated single words (letters only), e.g. "ClarityOS,Gmail"
        self.bias_list = os.environ.get("GNANI_STT_BIAS_WORDS", "").strip()
        self.bias_score = os.environ.get("GNANI_STT_BIAS_SCORE", "1.0")

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        if not self.api_key:
            raise STTRequestError("GNANI_API_KEY is not set")
        import httpx

        data = {"language_code": self.language, "format": self.format}
        if self.bias_list:
            data["bias_list"] = json.dumps([w.strip() for w in self.bias_list.split(",") if w.strip()])
            data["bias_score"] = self.bias_score
            data["format"] = "verbatim"  # boosting only works with verbatim output
        files = {"audio_file": ("speech.wav", pcm_to_wav(pcm, sample_rate), "audio/wav")}
        headers = {"X-API-Key-ID": self.api_key}

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                resp = await client.post(self.URL, data=data, files=files, headers=headers)
        except httpx.HTTPError as e:
            raise STTRequestError(f"Gnani STT network error: {e}") from e

        _raise_for_status("Gnani", resp)
        try:
            body = resp.json()
        except ValueError as e:
            raise STTRequestError("Gnani STT returned non-JSON response") from e

        if isinstance(body, dict) and body.get("success") is False:
            raise STTRequestError(f"Gnani STT error: {_error_message(resp)}")
        text = (body.get("transcript") or "").strip() if isinstance(body, dict) else ""
        if not text:
            raise NoSpeechError("Gnani STT returned an empty transcript")
        return text


# ═══════════════════════════════════════════════════════════════
#  Sarvam (Saaras / Saarika)
# ═══════════════════════════════════════════════════════════════

class SarvamSTT(STTProvider):
    """
    REST: POST /speech-to-text (multipart). Handles Indian languages + English and
    code-mixed speech. Default model saaras:v3 (mode "transcribe"); set
    SARVAM_STT_MODE=codemix for Hinglish-style output.
    If CLARITYOS_STT_LANGUAGE is unset the model auto-detects the language.
    """
    name = "sarvam"
    URL = "https://api.sarvam.ai/speech-to-text"

    def __init__(self, api_key: Optional[str] = None, language: Optional[str] = None):
        self.api_key = api_key or os.environ.get("SARVAM_API_KEY", "")
        self.language = language or os.environ.get("CLARITYOS_STT_LANGUAGE", "")
        self.model = os.environ.get("SARVAM_STT_MODEL", "saaras:v3")
        self.mode = os.environ.get("SARVAM_STT_MODE", "transcribe")

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        if not self.api_key:
            raise STTRequestError("SARVAM_API_KEY is not set")
        import httpx

        data = {"model": self.model}
        if self.model.startswith("saaras"):
            data["mode"] = self.mode
        if self.language:
            data["language_code"] = self.language
        files = {"file": ("speech.wav", pcm_to_wav(pcm, sample_rate), "audio/wav")}
        headers = {"api-subscription-key": self.api_key}

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                resp = await client.post(self.URL, data=data, files=files, headers=headers)
        except httpx.HTTPError as e:
            raise STTRequestError(f"Sarvam STT network error: {e}") from e

        _raise_for_status("Sarvam", resp)
        try:
            body = resp.json()
        except ValueError as e:
            raise STTRequestError("Sarvam STT returned non-JSON response") from e
        text = (body.get("transcript") or "").strip() if isinstance(body, dict) else ""
        if not text:
            raise NoSpeechError("Sarvam STT returned an empty transcript")
        return text


# ═══════════════════════════════════════════════════════════════
#  Fallback wrapper
# ═══════════════════════════════════════════════════════════════

class FallbackSTT(STTProvider):
    """Try `primary`; on a request/auth/network failure (NOT on 'no speech') try `secondary`."""

    def __init__(self, primary: STTProvider, secondary: STTProvider):
        self.primary = primary
        self.secondary = secondary
        self.name = f"{primary.name}+{secondary.name}"

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        try:
            return await self.primary.transcribe(pcm, sample_rate)
        except STTRequestError as e:
            print(f"[STT] {self.primary.name} failed ({e}); falling back to {self.secondary.name}")
            return await self.secondary.transcribe(pcm, sample_rate)


# ═══════════════════════════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════════════════════════

_PROVIDERS = {"google": GoogleSTT, "gnani": GnaniSTT, "sarvam": SarvamSTT}
_singleton: Optional[STTProvider] = None


def build_stt_provider() -> STTProvider:
    primary_name = os.environ.get("CLARITYOS_STT_PROVIDER", "google").strip().lower()
    if primary_name not in _PROVIDERS:
        print(f"[STT] Unknown CLARITYOS_STT_PROVIDER '{primary_name}' — using google")
        primary_name = "google"
    primary = _PROVIDERS[primary_name]()

    fallback_name = os.environ.get(
        "CLARITYOS_STT_FALLBACK", "google" if primary_name != "google" else ""
    ).strip().lower()
    if fallback_name and fallback_name != primary_name and fallback_name in _PROVIDERS:
        provider: STTProvider = FallbackSTT(primary, _PROVIDERS[fallback_name]())
    else:
        provider = primary
    print(f"[STT] Engine: {provider.name}")
    return provider


def get_stt_provider() -> STTProvider:
    global _singleton
    if _singleton is None:
        _singleton = build_stt_provider()
    return _singleton


def reset_stt_provider() -> None:
    """For tests / hot config reload."""
    global _singleton
    _singleton = None
