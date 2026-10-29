"""
ClarityOS — Alternative TTS engines
===================================
Used by voice/tts.py when CLARITYOS_TTS_PROVIDER is not "google".

    CLARITYOS_TTS_PROVIDER = google | gnani | sarvam | windows   (default: google)

"windows" is the built-in Windows synthesizer: no API key, no Google Cloud
credentials, works offline. voice/tts.py falls back to it automatically when the
configured cloud engine cannot start, so the assistant is never silent.
    CLARITYOS_TTS_WINDOWS_VOICE   optional installed voice name (e.g. "Microsoft Zira Desktop")
    CLARITYOS_TTS_SPEED           0.5–2.0, mapped onto SAPI's -10..10 rate scale

Gnani Timbre   POST https://api.vachana.ai/api/v1/tts/inference   (returns binary WAV)
    GNANI_API_KEY            required (sent as X-API-Key-ID)
    GNANI_TTS_MODEL          timbre-v2.5 (default) | timbre-v3.0
    GNANI_TTS_VOICE          Poorvi (default; Hinglish voice for timbre-v2.5)
                             Voices are tied to a model — timbre-v3.0 voices (e.g. Shivani,
                             Avinash) speak all 10 languages; v2.5 voices have one preferred
                             language each (Nalini = Hindi, Kaveri = English, ...).
    GNANI_TTS_LANGUAGE       hi-en (default, Hinglish) | hi-IN | en-IN | ta-IN | ... | auto
    GNANI_TTS_SPEED          1.0
    GNANI_TTS_SAMPLE_RATE    48000

Sarvam Bulbul  POST https://api.sarvam.ai/text-to-speech          (JSON, base64 WAV in `audios`)
    SARVAM_API_KEY           required (sent as api-subscription-key)
    SARVAM_TTS_MODEL         bulbul:v3 (default)
    SARVAM_TTS_SPEAKER       shubh (default)
    SARVAM_TTS_PACE          speaking speed 0.5–2.0 (optional)
    SARVAM_TTS_LANGUAGE      hi-IN (default) — controls number/abbreviation handling;
                             code-mixed Hinglish text is fine.

Every provider returns WAV bytes, which the Electron renderer plays through
AudioContext.decodeAudioData (Chromium decodes WAV natively).
"""

from __future__ import annotations

import base64
import os
import re
from typing import Optional

HTTP_TIMEOUT_S = float(os.environ.get("CLARITYOS_TTS_TIMEOUT", "30"))

# Per-request character budget for each engine (sentences are packed up to this size).
MAX_CHARS = {"gnani": 1500, "sarvam": 2500, "windows": 2000}


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


class GnaniTTS:
    name = "gnani"
    URL = "https://api.vachana.ai/api/v1/tts/inference"

    def __init__(self):
        self.api_key = os.environ.get("GNANI_API_KEY", "")
        self.model = os.environ.get("GNANI_TTS_MODEL", "timbre-v2.5")
        self.voice = os.environ.get("GNANI_TTS_VOICE", "Poorvi")
        self.language = os.environ.get("GNANI_TTS_LANGUAGE", "hi-en")
        self.speed = float(os.environ.get("GNANI_TTS_SPEED", "1.0"))
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

    async def synthesize(self, text: str) -> Optional[bytes]:
        if not self.api_key:
            raise TTSProviderError("GNANI_API_KEY is not set")
        import httpx

        headers = {"X-API-Key-ID": self.api_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                resp = await client.post(self.URL, json=self.build_payload(text), headers=headers)
        except httpx.HTTPError as e:
            raise TTSProviderError(f"Gnani TTS network error: {e}") from e
        if resp.status_code >= 400:
            raise TTSProviderError(f"Gnani TTS HTTP {resp.status_code}: {_err(resp)}")
        audio = resp.content
        if not audio or len(audio) < 64:
            raise TTSProviderError("Gnani TTS returned empty audio")
        return audio


class SarvamTTS:
    name = "sarvam"
    URL = "https://api.sarvam.ai/text-to-speech"

    def __init__(self):
        self.api_key = os.environ.get("SARVAM_API_KEY", "")
        self.model = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")
        self.speaker = os.environ.get("SARVAM_TTS_SPEAKER", "shubh")
        self.language = os.environ.get("SARVAM_TTS_LANGUAGE", "hi-IN")

    def build_payload(self, text: str) -> dict:
        language = self.language
        if language == "auto-script":
            language = detect_language(text)
        payload = {
            "text": text[: MAX_CHARS["sarvam"]],
            "model": self.model,
            "target_language_code": language,
            "speaker": self.speaker,
        }
        pace = os.environ.get("SARVAM_TTS_PACE", "").strip()
        if pace:
            try:
                payload["pace"] = max(0.5, min(2.0, float(pace)))
            except ValueError:
                pass
        return payload

    async def synthesize(self, text: str) -> Optional[bytes]:
        if not self.api_key:
            raise TTSProviderError("SARVAM_API_KEY is not set")
        import httpx

        headers = {"api-subscription-key": self.api_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                resp = await client.post(self.URL, json=self.build_payload(text), headers=headers)
        except httpx.HTTPError as e:
            raise TTSProviderError(f"Sarvam TTS network error: {e}") from e
        if resp.status_code >= 400:
            raise TTSProviderError(f"Sarvam TTS HTTP {resp.status_code}: {_err(resp)}")
        try:
            audios = resp.json().get("audios") or []
        except ValueError as e:
            raise TTSProviderError("Sarvam TTS returned non-JSON response") from e
        if not audios:
            raise TTSProviderError("Sarvam TTS returned no audio")
        # Docs: join the base64 strings, then decode once.
        return base64.b64decode("".join(audios))


# ═══════════════════════════════════════════════════════════════
#  Windows SAPI — local, offline, no credentials
# ═══════════════════════════════════════════════════════════════

class WindowsSapiTTS:
    """
    Speech via the Windows built-in synthesizer (System.Speech).

    This is the only engine that needs no API key and no Google Cloud
    credentials, so it is what keeps the assistant audible out of the box.
    Quality is below the cloud voices — configure Gnani/Sarvam/Google for those.

    Runs PowerShell rather than binding COM so it needs no extra Python
    dependency (pywin32 is not installed). The text is passed as base64 and
    decoded inside PowerShell, so nothing a user or model says can be
    interpreted as script.
    """

    name = "windows"

    def __init__(self):
        self.voice = os.environ.get("CLARITYOS_TTS_WINDOWS_VOICE", "").strip()
        try:
            self.speed = float(os.environ.get("CLARITYOS_TTS_SPEED", "1.0"))
        except (TypeError, ValueError):
            self.speed = 1.0

    @staticmethod
    def available() -> bool:
        return os.name == "nt"

    def _rate(self) -> int:
        """Map a 0.5–2.0 speed multiplier onto SAPI's -10..10 rate scale."""
        speed = min(max(self.speed, 0.5), 2.0)
        return int(round((speed - 1.0) * 10))

    def _script(self, text: str, out_path: str) -> str:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        voice_line = ""
        if self.voice:
            safe_voice = base64.b64encode(self.voice.encode("utf-8")).decode("ascii")
            voice_line = (
                f"$v=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{safe_voice}'));"
                "try{$s.SelectVoice($v)}catch{};"
            )
        return (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"{voice_line}"
            f"$s.Rate={self._rate()};"
            f"$s.SetOutputToWaveFile('{out_path}');"
            f"$t=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{payload}'));"
            "$s.Speak($t);$s.Dispose();"
        )

    def _blocking_synthesize(self, text: str) -> bytes:
        import subprocess
        import tempfile

        fd, out_path = tempfile.mkstemp(suffix=".wav", prefix="clarityos_tts_")
        os.close(fd)
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 self._script(text, out_path.replace("'", "''"))],
                capture_output=True, text=True, timeout=HTTP_TIMEOUT_S,
                creationflags=0x08000000,       # CREATE_NO_WINDOW
            )
            if proc.returncode != 0:
                raise TTSProviderError(
                    f"Windows speech failed ({proc.returncode}): "
                    f"{(proc.stderr or '').strip()[:200]}")
            with open(out_path, "rb") as f:
                audio = f.read()
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        if not audio.startswith(b"RIFF"):
            raise TTSProviderError("Windows speech produced no audio")
        return audio

    async def synthesize(self, text: str) -> Optional[bytes]:
        import asyncio

        if not self.available():
            raise TTSProviderError("Windows speech is only available on Windows")
        clean = (text or "").strip()
        if not clean:
            return None
        return await asyncio.get_running_loop().run_in_executor(
            None, self._blocking_synthesize, clean[: MAX_CHARS["windows"]])


def build_provider(name: str):
    name = (name or "").strip().lower()
    if name == "gnani":
        return GnaniTTS()
    if name == "sarvam":
        return SarvamTTS()
    if name in ("windows", "sapi", "local"):
        return WindowsSapiTTS()
    return None


def build_local_provider():
    """
    The engine used when no cloud provider is configured or reachable.
    Returns None on non-Windows platforms.
    """
    return WindowsSapiTTS() if WindowsSapiTTS.available() else None
