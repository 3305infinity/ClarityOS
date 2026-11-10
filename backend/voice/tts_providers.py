"""
ClarityOS — Text-to-Speech engine (Gnani only)
==============================================
All speech synthesis runs on Gnani Timbre. There is no second vendor and no
switch to one: GNANI_API_KEY is what makes the assistant speak.

Gnani Timbre v2.5
    REST   POST https://api.vachana.ai/api/v1/tts/inference   (binary WAV)
    SSE    POST https://api.vachana.ai/api/v1/tts/sse         (progressive)

    GNANI_API_KEY            required (sent as X-API-Key-ID)
    GNANI_TTS_MODEL          timbre-v2.5 (default) | timbre-v3.0
    GNANI_TTS_VOICE          Poorvi (default; Hinglish voice for timbre-v2.5)
                             Voices are tied to a model — timbre-v3.0 voices (e.g. Shivani,
                             Avinash) speak all 10 languages; v2.5 voices have one preferred
                             language each (Nalini = Hindi, Kaveri = English, ...).
    GNANI_TTS_LANGUAGE       hi-en (default, Hinglish) | hi-IN | en-IN | ta-IN | ... | auto
    GNANI_TTS_SPEED          1.0  (clamped to Timbre's 0.85–1.15)
    GNANI_TTS_SAMPLE_RATE    48000

Returns WAV bytes, which the Electron renderer plays through
AudioContext.decodeAudioData (Chromium decodes WAV natively).
"""

from __future__ import annotations

import base64
import os
import re
from typing import Optional

HTTP_TIMEOUT_S = float(os.environ.get("CLARITYOS_TTS_TIMEOUT", "30"))

# Per-request character budget for each engine (sentences are packed up to this size).
MAX_CHARS = {"gnani": 1500}

# How many requests an engine will accept at once. voice/tts.py synthesizes
# sentences concurrently, which is what lets the first one play while the rest
# are still being made — but Gnani documents "only 1 active request is allowed
# at a time" and answers a second one with HTTP 429 and no Retry-After header.
# Four parallel calls therefore lost sentences out of the middle of an answer.
# Engines not listed here keep the general limit in voice/tts.py.
MAX_CONCURRENT = {"gnani": 1}

# Gnani's limit is per account, not per call site, so REST and SSE streaming
# have to queue behind the *same* gate. Created lazily: a module-level
# asyncio primitive can otherwise end up bound to a loop that is no longer
# running after a settings reload rebuilds the engines.
_gnani_gate: Optional["asyncio.Semaphore"] = None


def gnani_gate() -> "asyncio.Semaphore":
    """The one semaphore every Gnani TTS request passes through."""
    global _gnani_gate
    if _gnani_gate is None:
        import asyncio
        _gnani_gate = asyncio.Semaphore(1)
    return _gnani_gate


# A 429 means someone else is mid-request, not that the text was bad. Wait a
# fixed moment and try again a bounded number of times; the docs state there is
# no Retry-After header to read, and retrying forever would hang the answer.
RATE_LIMIT_BACKOFF_S = 0.6
RATE_LIMIT_RETRIES = 3


class TTSProviderError(Exception):
    pass


def _err(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            e = data.get("error")
            if isinstance(e, dict):
                return str(e.get("message") or e)
            return str(e or data.get("message") or data.get("detail") or data)[:300]
    except Exception:
        pass
    return (resp.text or "")[:300]


# Rough script → language hint, used only when GNANI_TTS_LANGUAGE=auto-script
_SCRIPT_RANGES = [
    (0x0900, 0x097F, "hi-IN"),   # Devanagari (Hindi/Marathi — Hindi voice reads both acceptably)
    (0x0980, 0x09FF, "bn-IN"),
    (0x0A00, 0x0A7F, "pa-IN"),
    (0x0A80, 0x0AFF, "gu-IN"),
    (0x0B80, 0x0BFF, "ta-IN"),
    (0x0C00, 0x0C7F, "te-IN"),
    (0x0C80, 0x0CFF, "kn-IN"),
    (0x0D00, 0x0D7F, "ml-IN"),
]
_LATIN = re.compile(r"[A-Za-z]")


def detect_language(text: str, default: str = "en-IN") -> str:
    """Pick a language code from the dominant script in `text`."""
    counts: dict[str, int] = {}
    latin = 0
    for ch in text:
        cp = ord(ch)
        if _LATIN.match(ch):
            latin += 1
            continue
        for lo, hi, code in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[code] = counts.get(code, 0) + 1
                break
    if counts:
        code, n = max(counts.items(), key=lambda kv: kv[1])
        # Mostly-Latin text with a few Indic characters is still best read as English-India
        if n >= latin * 0.3:
            return code
    return default


# Timbre accepts 0.85–1.15 and answers 400 for anything outside it. The speed
# setting in the UI ranges wider than that, so a perfectly reasonable 1.3 on
# the slider turned into a failed synthesis here.
SPEED_MIN = 0.85
SPEED_MAX = 1.15

# Pinned rather than inherited. "timbre-v2.5 is the default" is a server-side
# default that can move; a silent jump to v3.0 would change the voice set under
# us, because voices are not shared between models.
DEFAULT_TTS_MODEL = "timbre-v2.5"


def wrap_pcm_wav(pcm: bytes, sample_rate: int, channels: int = 1, width: int = 2) -> bytes:
    """Give a bare PCM run its own WAV header."""
    import struct
    byte_rate = sample_rate * channels * width
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels,
                                    sample_rate, byte_rate, channels * width, width * 8)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def parse_wav_header(blob: bytes):
    """(sample_rate, channels, width, offset_of_audio) for a RIFF blob, or None.

    Reads the real format out of the stream instead of assuming the one we
    asked for, and reports where the samples start so the header can be
    dropped.
    """
    import struct
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        return None
    pos = 12
    fmt = None
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        try:
            size = struct.unpack_from("<I", blob, pos + 4)[0]
        except struct.error:
            return None
        body = pos + 8
        if cid == b"fmt " and body + 16 <= len(blob):
            _, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", blob, body)
            fmt = (rate, channels, max(1, bits // 8))
        elif cid == b"data":
            if fmt is None:
                return None
            return fmt[0], fmt[1], fmt[2], body
        pos = body + size + (size & 1)      # chunks are word-aligned
    return None


class PcmAligner:
    """Turns a run of SSE chunks into independently playable WAV buffers.

    Two things went wrong when each chunk was simply given its own header:

      * A chunk with an odd byte count left the 16-bit samples misaligned, and
        because every chunk was wrapped on its own the spare byte was dropped
        rather than carried — so the distortion continued into the next chunk.
        That is what made the voice crackle.
      * The first chunk's RIFF header describes the *whole* utterance, not that
        chunk, so passing it through unchanged handed the decoder a length that
        did not match the bytes present.

    So: parse the format once from the real header, strip it, and emit only
    whole samples, keeping the remainder for the next chunk.
    """

    def __init__(self, sample_rate: int, channels: int = 1, width: int = 2):
        self.sample_rate = sample_rate
        self.channels = channels
        self.width = width
        self._carry = b""
        self._saw_header = False

    @property
    def block_align(self) -> int:
        return max(1, self.channels * self.width)

    def feed(self, raw: bytes) -> Optional[bytes]:
        """A playable WAV for this chunk, or None when nothing whole is ready."""
        if not raw:
            return None
        if not self._saw_header:
            parsed = parse_wav_header(raw)
            if parsed:
                self.sample_rate, self.channels, self.width, offset = parsed
                raw = raw[offset:]
            self._saw_header = True

        buf = self._carry + raw
        usable = (len(buf) // self.block_align) * self.block_align
        self._carry = buf[usable:]
        if not usable:
            return None
        return wrap_pcm_wav(buf[:usable], self.sample_rate, self.channels, self.width)

    def flush(self) -> Optional[bytes]:
        """Whatever is left, padded to a whole sample. Called once at the end."""
        if not self._carry:
            return None
        pad = (-len(self._carry)) % self.block_align
        tail, self._carry = self._carry + b"\x00" * pad, b""
        return wrap_pcm_wav(tail, self.sample_rate, self.channels, self.width)


def clamp_speed(value) -> float:
    """A speed Timbre will accept, from whatever the settings hold."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return 1.0
    if speed < SPEED_MIN:
        return SPEED_MIN
    if speed > SPEED_MAX:
        return SPEED_MAX
    return speed


class GnaniTTS:
    name = "gnani"
    URL = "https://api.vachana.ai/api/v1/tts/inference"
    SSE_URL = "https://api.vachana.ai/api/v1/tts/sse"

    def __init__(self):
        self.api_key = os.environ.get("GNANI_API_KEY", "")
        self.model = os.environ.get("GNANI_TTS_MODEL", DEFAULT_TTS_MODEL) or DEFAULT_TTS_MODEL
        self.voice = os.environ.get("GNANI_TTS_VOICE", "Poorvi")
        self.language = os.environ.get("GNANI_TTS_LANGUAGE", "hi-en")
        raw_speed = os.environ.get("GNANI_TTS_SPEED", "1.0")
        self.speed = clamp_speed(raw_speed)
        if str(raw_speed).strip() not in ("", str(self.speed)):
            try:
                if abs(float(raw_speed) - self.speed) > 1e-9:
                    print(f"[TTS] Gnani speed {raw_speed} is outside "
                          f"{SPEED_MIN}-{SPEED_MAX}; using {self.speed}")
            except (TypeError, ValueError):
                print(f"[TTS] Gnani speed {raw_speed!r} is not a number; using {self.speed}")
        self.sample_rate = int(os.environ.get("GNANI_TTS_SAMPLE_RATE", "48000"))

    def build_payload(self, text: str) -> dict:
        language = self.language
        if language == "auto-script":
            language = detect_language(text)
        return {
            "text": text,
            "voice": self.voice,
            "model": self.model,
            "language": language,
            "speed": self.speed,
            "audio_config": {
                "sample_rate": self.sample_rate,
                "num_channels": 1,
                "sample_width": 2,
                "encoding": "linear_pcm",
                "container": "wav",
            },
        }

    def _headers(self) -> dict:
        return {"X-API-Key-ID": self.api_key, "Content-Type": "application/json"}

    async def synthesize(self, text: str) -> Optional[bytes]:
        if not self.api_key:
            raise TTSProviderError("GNANI_API_KEY is not set")
        import asyncio
        import httpx

        headers = self._headers()
        payload = self.build_payload(text)

        # One Gnani request at a time, shared with synthesize_stream().
        async with gnani_gate():
            last_429 = ""
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                        resp = await client.post(self.URL, json=payload, headers=headers)
                except httpx.HTTPError as e:
                    raise TTSProviderError(f"Gnani TTS network error: {e}") from e

                # Another request of ours is still in flight somewhere. Waiting
                # briefly is the documented remedy — there is no Retry-After to
                # honour — but only a bounded number of times.
                if resp.status_code == 429:
                    last_429 = _err(resp)
                    if attempt < RATE_LIMIT_RETRIES:
                        await asyncio.sleep(RATE_LIMIT_BACKOFF_S)
                        continue
                    # Keep the status code in the message: callers and logs
                    # identify provider failures by it.
                    raise TTSProviderError(
                        f"Gnani TTS HTTP 429 after {RATE_LIMIT_RETRIES} retries: {last_429}")

                if resp.status_code >= 400:
                    raise TTSProviderError(f"Gnani TTS HTTP {resp.status_code}: {_err(resp)}")
                audio = resp.content
                if not audio or len(audio) < 64:
                    raise TTSProviderError("Gnani TTS returned empty audio")
                return audio
        return None

    async def synthesize_stream(self, text: str):
        """Yield (chunk_index, audio_bytes, is_final) as Timbre produces them.

        Server-Sent Events from POST /api/v1/tts/sse. The body is the same
        TextToSpeechRequest the /inference endpoint takes, so there is one
        payload builder for both. Audio starts arriving while the rest of the
        sentence is still being synthesized, which is the entire point — so
        chunks are forwarded as they land, not collected into one buffer.

        Passes through the same gate as synthesize(): the documented limit is
        one active request per account, whichever endpoint it is on.
        """
        if not self.api_key:
            raise TTSProviderError("GNANI_API_KEY is not set")
        import asyncio
        import httpx

        payload = self.build_payload(text)
        headers = dict(self._headers())
        headers["Accept"] = "text/event-stream"

        async with gnani_gate():
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                        async with client.stream("POST", self.SSE_URL, json=payload,
                                                 headers=headers) as resp:
                            if resp.status_code == 429:
                                await resp.aread()
                                if attempt < RATE_LIMIT_RETRIES:
                                    await asyncio.sleep(RATE_LIMIT_BACKOFF_S)
                                    continue
                                raise TTSProviderError(
                                    f"Gnani TTS SSE HTTP 429 after "
                                    f"{RATE_LIMIT_RETRIES} retries: {_err(resp)}")
                            if resp.status_code >= 400:
                                await resp.aread()
                                raise TTSProviderError(
                                    f"Gnani TTS SSE HTTP {resp.status_code}: {_err(resp)}")

                            # One aligner per stream: the carry has to survive
                            # across chunks or the spare byte is lost and the
                            # samples shift.
                            aligner = PcmAligner(self.sample_rate)
                            last_index = 0
                            async for index, raw, is_final in self._read_sse(resp):
                                last_index = index
                                playable = aligner.feed(raw)
                                if playable:
                                    yield index, playable, False
                            tail = aligner.flush()
                            if tail:
                                last_index += 1
                                yield last_index, tail, False
                            # The final marker carries no audio of its own, so
                            # it is emitted once the buffer has drained.
                            yield last_index + 1, b"", True
                            return
                except httpx.HTTPError as e:
                    raise TTSProviderError(f"Gnani TTS SSE network error: {e}") from e

    @staticmethod
    async def _read_sse(resp):
        """Parse the documented start / chunk / complete / error events."""
        import json as _json

        event = ""
        async for line in resp.aiter_lines():
            line = line.rstrip("\r")
            if not line:
                event = ""                     # blank line ends an event block
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            try:
                data = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue

            if event == "error" or data.get("type") == "error":
                raise TTSProviderError(
                    f"Gnani TTS SSE error: {data.get('message') or 'unknown'}")
            if event == "start":
                continue                        # status/text only, no audio
            if event in ("chunk", "complete"):
                index = int(data.get("chunk_index", 0) or 0)
                is_final = bool(data.get("is_final", event == "complete"))
                encoded = data.get("audio") or ""
                audio = base64.b64decode(encoded) if encoded else b""
                yield index, audio, is_final
                if is_final:
                    return

def build_provider(name: str = "gnani"):
    """The TTS engine. Always Gnani Timbre — the argument is accepted so the
    existing call site needs no edit, and ignored because there is nothing
    else to build."""
    return GnaniTTS()
