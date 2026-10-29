"""
Regression: Electron TTS chunks must survive exactly one JSON.parse.

`TTSChunk.to_ws_message()` returns already-serialized JSON. The voice server
used to wrap it in `json.dumps()` a second time, so the renderer's single
`JSON.parse(event.data)` produced a *string* instead of an object —
`msg.type` was undefined, the `tts_chunk` branch never ran, and the desktop
app played no audio at all.
"""
import base64
import json
import inspect

import pytest

from voice.tts import TTSChunk


AUDIO = b"OggS\x00\x02fake-opus-payload\x00\xff"


# ── the serializer's contract ───────────────────────────────────

def test_to_ws_message_returns_serialized_json_text():
    """It returns a str, not a dict. Callers must send it as-is."""
    raw = TTSChunk(audio=AUDIO, seq=0, final=False, text="hello").to_ws_message()
    assert isinstance(raw, str)
    assert json.loads(raw)["type"] == "tts_chunk"


def test_single_parse_yields_a_usable_object():
    """One parse — exactly what the renderer does — must give an object."""
    raw = TTSChunk(audio=AUDIO, seq=3, final=True, text="bye").to_ws_message()

    msg = json.loads(raw)                      # renderer: JSON.parse(event.data)

    assert isinstance(msg, dict), "one parse must produce an object, not a string"
    assert msg["type"] == "tts_chunk"          # renderer: msg.type === "tts_chunk"
    assert msg["seq"] == 3
    assert msg["final"] is True
    assert base64.b64decode(msg["data"]) == AUDIO   # renderer: atob(data.data)


def test_double_encoding_is_what_the_bug_looked_like():
    """
    Pins the failure mode so a future caller re-introducing json.dumps() is
    recognisable: one parse yields a string and `.type` lookup fails.
    """
    raw = TTSChunk(audio=AUDIO, seq=0, final=False, text="hi").to_ws_message()

    broken = json.loads(json.dumps(raw))       # the old call site

    assert isinstance(broken, str)
    with pytest.raises(TypeError):
        broken["type"]                         # JS equivalent: msg.type === undefined


# ── the actual call site ────────────────────────────────────────

def test_voice_server_sends_the_chunk_without_re_encoding():
    """The Electron voice path must not wrap to_ws_message() in json.dumps()."""
    from servers import local_server

    source = inspect.getsource(local_server.VoiceAssistant._stream_tts)
    assert "chunk.to_ws_message()" in source
    assert "json.dumps(chunk.to_ws_message())" not in source
    assert "json.dumps(msg)" not in source, "double-encoding has been re-introduced"


@pytest.mark.asyncio
async def test_streamed_chunks_parse_once_and_keep_order(monkeypatch):
    """
    Drives the real `_stream_tts` with a stubbed engine and a fake socket, then
    parses each frame once — the renderer's exact behaviour.

    Also pins sequencing and end-of-stream: ascending seq, `final` on the last
    chunk only, and a trailing `tts_done`.
    """
    from servers import local_server

    sentences = [b"OggS-one", b"OggS-two", b"OggS-three"]

    class FakeEngine:
        async def stream_synthesize(self, text):
            for i, audio in enumerate(sentences):
                yield TTSChunk(audio=audio, seq=i, final=(i == len(sentences) - 1),
                               text=f"sentence {i}")

    class FakeWS:
        def __init__(self):
            self.frames = []

        async def send(self, raw):
            assert isinstance(raw, str)
            self.frames.append(raw)

    monkeypatch.setattr(local_server, "get_tts_engine", lambda: FakeEngine())
    monkeypatch.setattr(local_server, "prepare_for_speech", lambda t: t)

    assistant = local_server.VoiceAssistant.__new__(local_server.VoiceAssistant)
    assistant.tts_playing = False
    ws = FakeWS()

    await local_server.VoiceAssistant._stream_tts(assistant, ws, "three sentences here")

    parsed = [json.loads(f) for f in ws.frames]          # exactly one parse each
    assert all(isinstance(m, dict) for m in parsed)

    chunks = [m for m in parsed if m["type"] == "tts_chunk"]
    assert len(chunks) == len(sentences), "every chunk must reach the renderer"
    assert [c["seq"] for c in chunks] == [0, 1, 2]
    assert [c["final"] for c in chunks] == [False, False, True]
    assert [base64.b64decode(c["data"]) for c in chunks] == sentences

    assert parsed[-1]["type"] == "tts_done", "end-of-stream marker must still be sent"

    # The mic gate must stay CLOSED here. It used to clear as soon as the last
    # chunk was sent, which un-muted the microphone while the speakers still
    # had seconds of audio to play — the assistant then heard itself and
    # echoed. It is now released by the renderer's `tts_done`, with a watchdog
    # so it can never latch shut.
    assert assistant.tts_playing is True, "gate must hold until playback actually ends"
    assert assistant._tts_deadline > 0, "watchdog must be armed"
    assistant._release_tts_gate("test: renderer finished")
    assert assistant.tts_playing is False


@pytest.mark.asyncio
async def test_stream_tts_skips_empty_text(monkeypatch):
    """Unchanged behaviour: nothing is sent for empty/too-short text."""
    from servers import local_server

    class FakeWS:
        def __init__(self):
            self.frames = []

        async def send(self, raw):
            self.frames.append(raw)

    assistant = local_server.VoiceAssistant.__new__(local_server.VoiceAssistant)
    assistant.tts_playing = False
    ws = FakeWS()

    await local_server.VoiceAssistant._stream_tts(assistant, ws, "  ")
    assert ws.frames == []


# ── the hub path is a different contract and must stay untouched ─

def test_hub_broadcasts_a_dict_not_a_string():
    """
    hub/core.py builds its own tts_chunk dict and hands it to broadcast(), which
    serializes once. This fix must not have changed that path.
    """
    from hub import core

    source = inspect.getsource(core.Hub._speak)
    assert '"type": "tts_chunk"' in source
    assert "to_ws_message" not in source, "the hub path should not use the string serializer"
