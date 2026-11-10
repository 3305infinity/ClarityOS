"""
ClarityOS — Speech-to-Text (Gnani only)
=======================================
All speech recognition runs on Gnani Vachana. There is no second vendor and no
switch to one: GNANI_API_KEY is what makes the microphone work.

    CLARITYOS_STT_PROVIDER = gnani-stream | gnani   (default: gnani-stream)
    CLARITYOS_STT_LANGUAGE = BCP-47 code, e.g. en-IN, hi-IN, ta-IN

Engines
  gnani-stream  Prisma v2.5 over the realtime socket (default)
                  wss://api.vachana.ai/stt/v3/stream
                  Audio goes out while the user is still speaking, so the
                  transcript is ready almost as soon as they stop. Falls back
                  to the REST engine below if the socket cannot be used — that
                  is the only fallback, and it is still Gnani.
  gnani         Prisma v2.5 over REST
                  POST https://api.vachana.ai/stt/v3
                  key: GNANI_API_KEY  (sent as X-API-Key-ID)
                  en-IN accepts English+Hindi mixed speech and answers in Latin
                  script; hi-IN answers in Devanagari. Clips are capped at 30s,
                  the documented maximum for this endpoint.

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

# Gnani's documented ceiling for POST /stt/v3. Longer clips are refused by the
# API, so the recorder stops at this length rather than uploading something
# that cannot succeed.
GNANI_MAX_AUDIO_S = 30.0

# Documented values for bias_score. Anything else is a 400, so an out-of-range
# setting is reported here instead of being sent.
GNANI_BIAS_SCORES = ("0.0", "0.5", "1.0", "1.5", "2.0")

# Pinned rather than inherited from the server-side default.
GNANI_STT_MODEL = "prisma-v2.5"


def pcm_duration_s(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    """Seconds of 16-bit mono PCM in `pcm`."""
    if sample_rate <= 0:
        return 0.0
    return len(pcm) / 2.0 / float(sample_rate)


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
#  Gnani Vachana (Prisma v2.5)
# ═══════════════════════════════════════════════════════════════

class GnaniSTT(STTProvider):
    """
    REST: POST /stt/v3 (multipart). Clips are capped at 30 s — the documented
    maximum for this endpoint. Audio is auto-converted to 16 kHz mono
    server-side.
    """
    name = "gnani"
    URL = "https://api.vachana.ai/stt/v3"

    def __init__(self, api_key: Optional[str] = None, language: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GNANI_API_KEY", "")
        self.language = language or os.environ.get("CLARITYOS_STT_LANGUAGE", "en-IN")
        self.model = os.environ.get("GNANI_STT_MODEL", GNANI_STT_MODEL) or GNANI_STT_MODEL
        # "transcribe" = ITN on: "five thousand rupees" -> "₹5,000". Better for commands.
        # "verbatim"   = raw spoken form.
        self.format = os.environ.get("GNANI_STT_FORMAT", "transcribe")
        # Optional word boosting: comma-separated single words (letters only), e.g. "ClarityOS,Gmail"
        self.bias_list = os.environ.get("GNANI_STT_BIAS_WORDS", "").strip()
        self.bias_score = self._valid_bias_score(
            os.environ.get("GNANI_STT_BIAS_SCORE", "1.0"))

    @staticmethod
    def _valid_bias_score(raw: str) -> str:
        """A documented bias_score, or "" to disable boosting.

        Only 0.0/0.5/1.0/1.5/2.0 are accepted by the API; sending 0.7 fails the
        whole request, which would cost the user the transcript rather than
        just the boosting.
        """
        value = str(raw or "").strip()
        if not value:
            return ""
        try:
            normalized = f"{float(value):.1f}"
        except (TypeError, ValueError):
            print(f"[STT] Gnani bias_score {raw!r} is not a number; boosting disabled")
            return ""
        if normalized not in GNANI_BIAS_SCORES:
            print(f"[STT] Gnani bias_score {raw} is not one of "
                  f"{', '.join(GNANI_BIAS_SCORES)}; boosting disabled")
            return ""
        return normalized

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        if not self.api_key:
            raise STTRequestError("GNANI_API_KEY is not set")
        import httpx

        duration = pcm_duration_s(pcm, sample_rate)
        if duration > GNANI_MAX_AUDIO_S:
            raise STTRequestError(
                f"The clip is {duration:.1f}s long and Gnani accepts at most "
                f"{GNANI_MAX_AUDIO_S:.0f}s. Say it again in a shorter take.")

        data = {
            "language_code": self.language,
            "format": self.format,
            "model": self.model,
        }
        if self.bias_list and self.bias_score:
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
#  Gnani Vachana realtime (Prisma v2.5 over WebSocket)
# ═══════════════════════════════════════════════════════════════

# The socket accepts raw PCM only, in fixed-size frames: 512 samples of signed
# 16-bit little-endian mono, i.e. exactly 1024 bytes. Anything else is refused.
GNANI_FRAME_BYTES = 1024
GNANI_STREAM_URL = "wss://api.vachana.ai/stt/v3/stream"

# Frames are meant to arrive at the pace they were spoken: one per 32 ms at
# 16 kHz. Buffering and bursting defeats the server's voice detection.
GNANI_FRAME_INTERVAL_S = 0.032


def iter_frames(pcm: bytes, frame_bytes: int = GNANI_FRAME_BYTES):
    """Split PCM into exactly `frame_bytes`-sized frames, dropping any remainder.

    The tail is dropped rather than zero-padded: a short final frame is not a
    legal frame, and padding it would feed the recogniser silence it never
    heard. Callers keep the remainder and prepend it to the next buffer.
    """
    for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        yield pcm[start:start + frame_bytes]


class GnaniStreamSession:
    """One realtime utterance: open, push PCM, commit, read the transcript.

    Utterance end is decided in exactly one place. The server's own voice
    activity detection segments speech and emits `transcript` events; the
    client only sends `commit` when the user has explicitly finished (released
    push-to-talk, or the clip ceiling was hit). Running the local RMS silence
    detector as well would cut takes early and race the server's segments,
    which is why local_server leaves it switched off while this is in use.
    """

    def __init__(self, api_key: str, language: str, model: str,
                 sample_rate: int = SAMPLE_RATE):
        self.api_key = api_key
        self.language = language
        self.model = model
        self.sample_rate = sample_rate
        self._ws = None
        self._reader = None
        self._carry = b""              # bytes left over below one full frame
        self._segments: list[str] = []
        self._error = ""
        self._closed = False

    def _headers(self) -> dict:
        # Documented upgrade headers. Nothing else is sent.
        return {
            "x-api-key-id": self.api_key,
            "language_code": self.language,
            "model": self.model,
            "x-sample-rate": str(self.sample_rate),
        }

    async def open(self) -> None:
        if not self.api_key:
            raise STTRequestError("GNANI_API_KEY is not set")
        import asyncio
        try:
            from websockets.asyncio.client import connect
        except ImportError as e:      # pragma: no cover - dependency is present in requirements
            raise STTRequestError(f"websockets is required for streaming STT: {e}") from e
        try:
            self._ws = await connect(GNANI_STREAM_URL, additional_headers=self._headers())
        except Exception as e:
            # A refused handshake is a 401 for a bad key, or no socket at all.
            raise STTRequestError(f"Gnani streaming STT could not connect: {e}") from e
        self._reader = asyncio.create_task(self._read_events())

    async def _read_events(self) -> None:
        """Consume documented server events until the socket closes."""
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue                      # server sends JSON text only
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                kind = str(event.get("type", ""))
                if kind == "connected":
                    print("[STT] Gnani stream connected "
                          f"(chunk_size={event.get('config', {}).get('chunk_size')})")
                elif kind == "speech_start":
                    print("[STT] Gnani stream: speech detected")
                elif kind == "processing":
                    print("[STT] Gnani stream: transcribing segment")
                elif kind == "transcript":
                    text = str(event.get("text", "") or "").strip()
                    if text:
                        self._segments.append(text)
                elif kind == "error":
                    # Recoverable per the docs — the session stays open — so
                    # record it and let the caller decide.
                    self._error = str(event.get("message", "") or "stream error")
                    print(f"[STT] Gnani stream error: {self._error}")
        except Exception as e:
            if not self._closed:
                self._error = self._error or f"{e.__class__.__name__}: {e}"

    async def send_pcm(self, pcm: bytes) -> None:
        """Queue PCM, emitting only whole 1024-byte frames."""
        if self._ws is None:
            return
        import asyncio
        buf = self._carry + bytes(pcm)
        sent = 0
        for frame in iter_frames(buf):
            await self._ws.send(frame)
            sent += 1
            # Pace the frames so the server's VAD sees real-time audio.
            if sent % 8 == 0:
                await asyncio.sleep(0)
        used = (len(buf) // GNANI_FRAME_BYTES) * GNANI_FRAME_BYTES
        self._carry = buf[used:]

    async def commit(self) -> None:
        """Close the open utterance now instead of waiting for silence."""
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"type": "commit"}))
        except Exception as e:
            print(f"[STT] Gnani stream commit failed: {e}")

    async def finish(self, settle_s: float = 2.5) -> str:
        """Commit, wait briefly for the final transcript, close, return the text."""
        import asyncio
        await self.commit()
        deadline = asyncio.get_event_loop().time() + settle_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            if self._segments and self._carry == b"":
                # Give a little room for a second segment, then stop waiting.
                await asyncio.sleep(0.25)
                break
        await self.close()
        if self._segments:
            return " ".join(self._segments).strip()
        if self._error:
            raise STTRequestError(f"Gnani streaming STT: {self._error}")
        raise NoSpeechError("Gnani streaming STT returned no transcript")

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None


class GnaniStreamingSTT(STTProvider):
    """Realtime Prisma v2.5, with the REST engine as its own fallback.

    Keeps the batch `transcribe()` contract the servers already use, so nothing
    downstream has to know whether the audio went over a socket or a POST.
    `local_server` additionally drives `session()` directly to send audio while
    the user is still talking, which is where the latency win actually comes
    from.
    """
    name = "gnani-stream"

    def __init__(self, api_key: Optional[str] = None, language: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GNANI_API_KEY", "")
        self.language = language or os.environ.get("CLARITYOS_STT_LANGUAGE", "en-IN")
        self.model = os.environ.get("GNANI_STT_MODEL", GNANI_STT_MODEL) or GNANI_STT_MODEL
        self._rest = GnaniSTT(api_key=self.api_key, language=self.language)

    def session(self, sample_rate: int = SAMPLE_RATE) -> GnaniStreamSession:
        return GnaniStreamSession(self.api_key, self.language, self.model, sample_rate)

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        """Stream an already-captured buffer; fall back to REST if that fails."""
        session = self.session(sample_rate)
        try:
            await session.open()
            await session.send_pcm(pcm)
            return await session.finish()
        except NoSpeechError:
            raise
        except STTRequestError as e:
            print(f"[STT] streaming failed ({e}); using the REST endpoint")
            await session.close()
            return await self._rest.transcribe(pcm, sample_rate)


# ═══════════════════════════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════════════════════════

_PROVIDERS = {
    "gnani": GnaniSTT,
    "gnani-stream": GnaniStreamingSTT,
}
DEFAULT_PROVIDER = "gnani-stream"
_singleton: Optional[STTProvider] = None


def build_stt_provider() -> STTProvider:
    """The configured Gnani engine.

    There is no cross-vendor fallback to choose any more — speech recognition
    is Gnani. A setting naming an engine that no longer exists is reported and
    ignored rather than failing the microphone outright, because an old
    settings.json or a stale .env should not leave the user unable to speak.
    """
    name = os.environ.get("CLARITYOS_STT_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if name not in _PROVIDERS:
        print(f"[STT] CLARITYOS_STT_PROVIDER '{name}' is not available "
              f"(voice runs on Gnani) — using {DEFAULT_PROVIDER}")
        name = DEFAULT_PROVIDER
    provider = _PROVIDERS[name]()
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
