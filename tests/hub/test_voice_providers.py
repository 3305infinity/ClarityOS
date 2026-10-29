"""STT / TTS / Bolna adapters against mocked HTTP (contracts taken from the providers' public docs)."""
import asyncio
import base64
import json

import httpx
import pytest

from voice import stt, tts_providers
from voice.stt import GnaniSTT, SarvamSTT, FallbackSTT, NoSpeechError, STTRequestError, pcm_to_wav

PCM = b"\x01\x02" * 8000
REAL = httpx.AsyncClient


def mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL(transport=transport, **kw))


def run(coro):
    return asyncio.run(coro)


def test_gnani_stt_request_and_parse(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("x-api-key-id")
        body = req.content
        seen["has_lang"] = b'name="language_code"' in body and b"en-IN" in body
        seen["has_file"] = b'name="audio_file"' in body and b"RIFF" in body
        return httpx.Response(200, json={"success": True, "transcript": "Chrome kholo", "request_id": "1"})

    mock_http(monkeypatch, handler)
    out = run(GnaniSTT(api_key="K", language="en-IN").transcribe(PCM))
    assert out == "Chrome kholo"
    assert seen == {"url": "https://api.vachana.ai/stt/v3", "key": "K", "has_lang": True, "has_file": True}


def test_gnani_stt_errors(monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(200, json={"success": True, "transcript": "  "}))
    with pytest.raises(NoSpeechError):
        run(GnaniSTT(api_key="K").transcribe(PCM))
    mock_http(monkeypatch, lambda r: httpx.Response(403, json={"error": {"message": "no credits"}}))
    with pytest.raises(STTRequestError, match="403.*no credits"):
        run(GnaniSTT(api_key="K").transcribe(PCM))
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    with pytest.raises(STTRequestError, match="not set"):
        run(GnaniSTT(api_key="").transcribe(PCM))

    def boom(r):
        raise httpx.ConnectError("offline")
    mock_http(monkeypatch, boom)
    with pytest.raises(STTRequestError, match="network"):
        run(GnaniSTT(api_key="K").transcribe(PCM))


def test_sarvam_stt(monkeypatch):
    seen = {}

    def handler(req):
        seen["key"] = req.headers.get("api-subscription-key")
        seen["model"] = b"saaras:v3" in req.content
        return httpx.Response(200, json={"transcript": "नमस्ते दुनिया", "language_code": "hi-IN"})

    mock_http(monkeypatch, handler)
    assert run(SarvamSTT(api_key="S", language="").transcribe(PCM)) == "नमस्ते दुनिया"
    assert seen == {"key": "S", "model": True}


def test_fallback_only_on_request_errors(monkeypatch):
    class Bad:
        name = "bad"
        async def transcribe(self, pcm, sample_rate=16000):
            raise STTRequestError("down")

    class Quiet(Bad):
        async def transcribe(self, pcm, sample_rate=16000):
            raise NoSpeechError("silence")

    class Good:
        name = "good"
        async def transcribe(self, pcm, sample_rate=16000):
            return "fallback text"

    assert run(FallbackSTT(Bad(), Good()).transcribe(PCM)) == "fallback text"
    with pytest.raises(NoSpeechError):          # silence must not trigger a second (paid) call
        run(FallbackSTT(Quiet(), Good()).transcribe(PCM))


def test_factory_selection(monkeypatch):
    stt.reset_stt_provider()
    monkeypatch.setenv("CLARITYOS_STT_PROVIDER", "gnani")
    monkeypatch.delenv("CLARITYOS_STT_FALLBACK", raising=False)
    p = stt.get_stt_provider()
    assert p.name == "gnani+google"
    stt.reset_stt_provider()
    monkeypatch.setenv("CLARITYOS_STT_PROVIDER", "bogus")
    assert stt.get_stt_provider().name == "google"
    stt.reset_stt_provider()


def test_pcm_to_wav_header():
    w = pcm_to_wav(PCM)
    assert w[:4] == b"RIFF" and w[8:12] == b"WAVE"


# ───────── TTS ─────────

def test_gnani_tts(monkeypatch):
    monkeypatch.setenv("GNANI_API_KEY", "K")
    monkeypatch.setenv("GNANI_TTS_VOICE", "Poorvi")
    monkeypatch.setenv("GNANI_TTS_LANGUAGE", "hi-en")
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["key"] = req.headers["x-api-key-id"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"RIFF" + b"\x00" * 200, headers={"content-type": "audio/wav"})

    mock_http(monkeypatch, handler)
    audio = run(tts_providers.GnaniTTS().synthesize("Namaste, kaise ho?"))
    assert audio.startswith(b"RIFF")
    assert seen["url"] == "https://api.vachana.ai/api/v1/tts/inference" and seen["key"] == "K"
    b = seen["body"]
    assert (b["voice"], b["model"], b["language"]) == ("Poorvi", "timbre-v2.5", "hi-en")
    assert b["audio_config"]["container"] == "wav" and b["audio_config"]["encoding"] == "linear_pcm"


def test_sarvam_tts(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "S")
    monkeypatch.setenv("SARVAM_TTS_PACE", "1.3")
    wav = b"RIFF" + b"\x00" * 300
    half = base64.b64encode(wav).decode()
    mock_http(monkeypatch, lambda r: httpx.Response(200, json={"audios": [half[:40], half[40:]]}))
    assert run(tts_providers.SarvamTTS().synthesize("नमस्ते")) == wav
    p = tts_providers.SarvamTTS().build_payload("hi")
    assert p["model"] == "bulbul:v3" and p["speaker"] == "shubh" and p["pace"] == 1.3


def test_tts_http_error(monkeypatch):
    monkeypatch.setenv("GNANI_API_KEY", "K")
    mock_http(monkeypatch, lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(tts_providers.TTSProviderError, match="429.*slow down"):
        run(tts_providers.GnaniTTS().synthesize("hello"))


def test_detect_language():
    d = tts_providers.detect_language
    assert d("नमस्ते, आप कैसे हैं") == "hi-IN"
    assert d("Hello how are you") == "en-IN"
    assert d("வணக்கம் நண்பர்களே") == "ta-IN"
    assert d("Chrome खोलो please") in ("hi-IN", "en-IN")


def test_tts_engine_falls_back_to_google(monkeypatch):
    from voice import tts
    monkeypatch.setenv("CLARITYOS_TTS_PROVIDER", "gnani")
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    tts.reset_tts_engine()
    eng = tts.TTSEngine()
    assert eng.provider_name == "gnani"

    async def fake_google(self, text):
        return b"GOOGLE-AUDIO"
    monkeypatch.setattr(tts.TTSEngine, "_synthesize_google", fake_google)
    assert run(eng._synthesize_one("hi there")) == b"GOOGLE-AUDIO"      # no key -> fell back
    tts.reset_tts_engine()


def test_hindi_danda_sentence_split():
    from voice.tts import split_sentences
    parts = split_sentences("पहला वाक्य है। दूसरा वाक्य है। तीसरा।", 25)
    assert len(parts) >= 2


# ───────── Bolna ─────────

def test_bolna_call_and_status(monkeypatch):
    import tools.bolna_tools as b
    monkeypatch.setenv("BOLNA_API_KEY", "bk")
    monkeypatch.setenv("BOLNA_AGENT_ID", "123e4567-e89b-12d3-a456-426614174000")
    sent = {}

    def handler(req: httpx.Request):
        if req.method == "POST":
            sent["auth"] = req.headers["authorization"]
            sent["body"] = json.loads(req.content)
            return httpx.Response(200, json={"message": "done", "status": "queued", "execution_id": "e1e1e1e1-aaaa"})
        return httpx.Response(200, json={"status": "completed", "transcript": "agent: hello",
                                         "telephony_data": {"recording_url": "https://r/x.mp3", "duration": "12"}})

    mock_http(monkeypatch, handler)
    out = run(b.bolna_call("+91 98765-43210", user_data='{"name":"Asha"}'))
    assert "execution_id=e1e1e1e1-aaaa" in out
    assert sent["auth"] == "Bearer bk"
    assert sent["body"] == {"agent_id": "123e4567-e89b-12d3-a456-426614174000",
                            "recipient_phone_number": "+919876543210", "user_data": {"name": "Asha"}}
    st = json.loads(run(b.bolna_call_status("e1e1e1e1-aaaa")))
    assert st["status"] == "completed" and st["recording_url"] == "https://r/x.mp3"


def test_bolna_validation(monkeypatch):
    import tools.bolna_tools as b
    monkeypatch.setenv("BOLNA_API_KEY", "bk")
    monkeypatch.setenv("BOLNA_AGENT_ID", "123e4567-e89b-12d3-a456-426614174000")
    assert "international format" in run(b.bolna_call("9876543210"))
    assert "international format" in run(b.bolna_call("+0123"))
    monkeypatch.setenv("BOLNA_ALLOWED_PREFIXES", "+91")
    assert "BLOCKED" in run(b.bolna_call("+14155550123"))
    monkeypatch.delenv("BOLNA_AGENT_ID")
    monkeypatch.delenv("BOLNA_ALLOWED_PREFIXES")
    assert "no Bolna agent" in run(b.bolna_call("+919876543210"))
    monkeypatch.delenv("BOLNA_API_KEY")
    assert "not configured" in run(b.bolna_call("+919876543210"))


def test_bolna_requires_approval_and_is_selected_for_call_requests():
    from hub.policy import PolicyEngine
    p = PolicyEngine(lambda: {"permissions": {"calls": "allow"}, "auto_approve_sensitive": False})
    assert p.check("bolna_call", {"recipient_phone_number": "+91"}).action == "ask"   # sensitive even when 'allow'
    from tools.selector import ToolCategories
    assert "bolna_call" in ToolCategories.ALL
