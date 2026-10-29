"""
Moonwalk — Mac Client (Local Daemon)
======================================
Runs on the user's MacBook. Handles:
  1. Audio capture → Wake word detection → Speech-to-text (all local)
  2. Connects to the Cloud Orchestrator via WebSocket
  3. Sends transcribed text + desktop context to the cloud
  4. Receives tool execution requests from the cloud → executes locally via tools.py
  5. Streams UI updates from the cloud back to the Electron overlay
"""

import asyncio
import websockets
import json
import base64
import wave
import io
import struct
import numpy as np
import os
import sys
from typing import Optional
from functools import partial

# Force print to flush
print = partial(print, flush=True)

# Ensure the backend package root is on sys.path
_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# Voice libraries (optional — Mac Client can run in text-only mode without them)
try:
    import pvporcupine
except ImportError:
    pvporcupine = None  # type: ignore
    print("[Voice] pvporcupine not installed — wake word disabled")

try:
    import speech_recognition as sr
except ImportError:
    sr = None  # type: ignore
    print("[Voice] speech_recognition not installed — STT disabled")

# Local macOS tools & perception
from tools import registry as tool_registry
import agent.perception as perception

# ═══════════════════════════════════════════════════════════════
#  Configuration  
# ═══════════════════════════════════════════════════════════════

# Cloud Orchestrator WebSocket URL
CLOUD_URL = os.environ.get("MOONWALK_CLOUD_URL", "ws://localhost:8080")

# Picovoice Access Key for wake word
PICOVOICE_ACCESS_KEY = os.environ.get(
    "PICOVOICE_ACCESS_KEY",
    "lDvqq7J641WbqdzMsPCdLlawELhfGZOGhaceFzl3ZYYYzeeuXq55YA=="
)


def _map_user_action_to_text(action: str) -> str:
    normalized = (action or "").strip().lower()
    if not normalized:
        return ""
    if normalized == "approve_plan":
        return "proceed"
    if normalized == "cancel_plan":
        return "cancel"
    return normalized


# ═══════════════════════════════════════════════════════════════
#  Voice Processing (runs entirely on Mac)
# ═══════════════════════════════════════════════════════════════

class VoiceProcessor:
    """Handles wake word detection and speech-to-text locally."""

    def __init__(self):
        self.state = "IDLE"
        self.porcupine = None
        self.audio_buffer = bytearray()
        self.recognizer = sr.Recognizer()
        self.consecutive_silence_chunks = 0
        self.SILENCE_THRESHOLD_CHUNKS = 7
        self.MIN_BUFFER_SIZE = 16000 * 2 * 0.3
        self.grace_chunks_remaining = 0
        self.waiting_for_voice = False
        self.waiting_for_reply = False

        # Initialize Porcupine
        try:
            if PICOVOICE_ACCESS_KEY and PICOVOICE_ACCESS_KEY != "YOUR_KEY_HERE":
                project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
                custom_ppn = os.path.join(project_root, "hey_moonwalk.ppn")
                if os.path.exists(custom_ppn):
                    self.porcupine = pvporcupine.create(
                        access_key=PICOVOICE_ACCESS_KEY,
                        keyword_paths=[custom_ppn]
                    )
                    print("[Voice] Porcupine initialized with 'Hey Moonwalk'")
                else:
                    self.porcupine = pvporcupine.create(
                        access_key=PICOVOICE_ACCESS_KEY,
                        keywords=["porcupine"]
                    )
                    print("[Voice] Porcupine initialized with 'Porcupine' keyword")
        except Exception as e:
            print(f"[Voice] Porcupine init failed: {e}")

    def process_wake_word(self, pcm_data: bytes) -> bool:
        """Returns True if wake word detected."""
        if not self.porcupine:
            return False
        chunk_size = 1024
        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i:i+chunk_size]
            if len(chunk) == chunk_size:
                pcm_tuple = struct.unpack_from("h" * self.porcupine.frame_length, chunk)
                if self.porcupine.process(pcm_tuple) >= 0:
                    return True
        return False

    def transcribe(self, audio_buffer: bytearray) -> Optional[str]:
        """Convert buffered PCM audio to text using Google STT."""
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(audio_buffer)
        wav_io.seek(0)

        try:
            with sr.AudioFile(wav_io) as source:
                audio_data = self.recognizer.record(source)
            return self.recognizer.recognize_google(audio_data)
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            print(f"[Voice] STT error: {e}")
            return None


# ═══════════════════════════════════════════════════════════════
#  Mac Client — Connects to Cloud Orchestrator
# ═══════════════════════════════════════════════════════════════

class MacClient:
    """
    The local Mac daemon that:
    1. Connects to the Cloud Orchestrator
    2. Forwards Electron overlay messages
    3. Executes macOS tools on behalf of the cloud
    """

    def __init__(self):
        self.cloud_ws = None  # WebSocket to Cloud Orchestrator
        self.electron_ws = None  # WebSocket from Electron app
        self.voice = VoiceProcessor()

    async def connect_to_cloud(self):
        """Establish a persistent WebSocket connection to the Cloud Orchestrator."""
        while True:
            try:
                print(f"[Mac] Connecting to Cloud Orchestrator at {CLOUD_URL}...")
                async with websockets.connect(
                    CLOUD_URL,
                    ping_interval=120,
                    ping_timeout=600,
                    max_size=10 * 1024 * 1024,
                ) as cloud_ws:
                    # ── Auth handshake ──
                    # Load credentials (user_id + auth_token) from .env or cred file
                    auth_token = os.environ.get("MOONWALK_CLOUD_TOKEN", "")
                    user_id = os.environ.get("MOONWALK_USER_ID", "local-user")
                    await cloud_ws.send(json.dumps({
                        "type": "auth",
                        "method": "token",
                        "token": auth_token,
                        "user_id": user_id,
                    }))
                    # Wait for auth_result
                    try:
                        raw = await asyncio.wait_for(cloud_ws.recv(), timeout=15.0)
                        result = json.loads(raw)
                        if result.get("type") == "auth_result":
                            if not result.get("ok"):
                                print(f"[Mac] Auth rejected: {result.get('error')}")
                                await asyncio.sleep(5)
                                continue
                            print(f"[Mac] ✓ Auth OK — user_id='{result.get('user_id')}'")
                    except asyncio.TimeoutError:
                        print("[Mac] Auth timeout from cloud server")
                        continue

                    self.cloud_ws = cloud_ws
                    print(f"[Mac] ✓ Connected to Cloud Orchestrator!")

                    # Listen for messages from the cloud
                    async for message in cloud_ws:
                        await self.handle_cloud_message(json.loads(message))

            except websockets.exceptions.ConnectionClosed as e:
                print(f"[Mac] Cloud connection lost: {e}. Reconnecting in 3s...")
            except ConnectionRefusedError:
                print(f"[Mac] Cloud Orchestrator not reachable. Retrying in 5s...")
            except Exception as e:
                print(f"[Mac] Connection error: {e}. Retrying in 5s...")

            self.cloud_ws = None
            await asyncio.sleep(5)

    async def handle_cloud_message(self, data: dict):
        """Handle messages from the Cloud Orchestrator."""
        msg_type = data.get("type")

        if msg_type == "tool_request":
            # Cloud wants us to execute a macOS tool
            call_id = data.get("call_id", "")
            tool_name = data.get("tool_name", "")
            tool_args = data.get("tool_args", {})

            print(f"[Mac] Executing tool: {tool_name}({tool_args})")
            try:
                result = await tool_registry.execute(tool_name, tool_args)
            except Exception as e:
                result = f"Error executing {tool_name}: {e}"

            # Send result back to cloud
            if self.cloud_ws:
                await self.cloud_ws.send(json.dumps({
                    "type": "tool_response",
                    "call_id": call_id,
                    "result": result,
                }))

        elif msg_type in ("status", "thinking", "doing", "thought", "response", "progress"):
            # UI updates from cloud → forward to Electron overlay
            if self.electron_ws:
                try:
                    await self.electron_ws.send(json.dumps(data))
                except Exception:
                    pass

        elif msg_type == "await_reply":
            # Cloud agent is waiting for user to speak again
            self.voice.state = "LISTENING"
            self.voice.audio_buffer = bytearray()
            self.voice.consecutive_silence_chunks = 0
            self.voice.grace_chunks_remaining = 64
            self.voice.waiting_for_voice = True
            self.voice.waiting_for_reply = True

        elif msg_type == "pong":
            pass  # Keep-alive response

    async def handle_electron_message(self, websocket, data: dict):
        """Handle messages from the local Electron app."""
        msg_type = data.get("type")

        if msg_type == "audio_chunk":
            await self.process_audio(websocket, data.get("payload", ""))

        elif msg_type == "hotkey_pressed":
            print("=> HOTKEY PRESSED. Forcing wake...")
            self.voice.state = "LISTENING"
            self.voice.audio_buffer = bytearray()
            if self.electron_ws:
                await self.electron_ws.send(json.dumps({
                    "type": "status", "state": "state-listening"
                }))

        elif msg_type == "user_action":
            action = data.get("action", "")
            text = _map_user_action_to_text(action)
            if not text:
                return
            if not self.cloud_ws:
                print("[Mac] Not connected to Cloud! Cannot forward user_action.")
                return

            context = await perception.snapshot(text)
            context_data = {
                "active_app": context.active_app or "",
                "window_title": context.window_title or "",
                "browser_url": context.browser_url,
                "screen_text": context.screen_text,
            }
            await self.cloud_ws.send(json.dumps({
                "type": "user_action",
                "action": action,
                "text": text,
                "context": context_data,
            }))

    async def process_audio(self, websocket, b64_payload: str):
        """Process audio from Electron — wake word + STT, then send to cloud."""
        try:
            wav_bytes = base64.b64decode(b64_payload)
            with wave.open(io.BytesIO(wav_bytes), 'rb') as w:
                pcm_data = w.readframes(w.getnframes())

            if self.voice.state == "IDLE":
                if self.voice.process_wake_word(pcm_data):
                    print("=> WAKE WORD DETECTED!")
                    self.voice.state = "LISTENING"
                    self.voice.audio_buffer = bytearray()
                    await websocket.send(json.dumps({
                        "type": "status", "state": "state-listening"
                    }))

            elif self.voice.state == "LISTENING":
                # Grace period
                if self.voice.grace_chunks_remaining > 0:
                    self.voice.grace_chunks_remaining -= 1
                    return

                ints = np.frombuffer(pcm_data, dtype=np.int16)
                if len(ints) == 0:
                    return
                rms = np.sqrt(np.mean(ints.astype(np.float32)**2))

                # Wait for voice onset
                if self.voice.waiting_for_voice:
                    if rms > 1500:
                        print(f"[Audio] Voice detected (RMS={rms:.0f})")
                        self.voice.waiting_for_voice = False
                        self.voice.audio_buffer.extend(pcm_data)
                    return

                # Normal buffering
                self.voice.audio_buffer.extend(pcm_data)

                if rms < 250:
                    self.voice.consecutive_silence_chunks += 1
                else:
                    self.voice.consecutive_silence_chunks = 0

                if len(self.voice.audio_buffer) > self.voice.MIN_BUFFER_SIZE:
                    if self.voice.consecutive_silence_chunks >= self.voice.SILENCE_THRESHOLD_CHUNKS:
                        print("=> SILENCE. Transcribing...")
                        self.voice.state = "LOADING"
                        self.voice.consecutive_silence_chunks = 0

                        await websocket.send(json.dumps({
                            "type": "progress", "state": "state-loading"
                        }))

                        # Transcribe and send to cloud
                        asyncio.create_task(
                            self.transcribe_and_send(websocket)
                        )

        except Exception as e:
            print(f"[Audio] Error: {e}")

    async def transcribe_and_send(self, websocket):
        """Transcribe audio, capture context, send both to cloud."""
        text = self.voice.transcribe(self.voice.audio_buffer)

        if not text:
            if self.voice.waiting_for_reply:
                print("[Mac] STT failed, keeping mic open for reply...")
                self.voice.state = "LISTENING"
                self.voice.audio_buffer = bytearray()
                self.voice.waiting_for_voice = True
                return

            await websocket.send(json.dumps({
                "type": "response",
                "payload": {"text": "Sorry, I didn't catch that.", "app": ""}
            }))
            await asyncio.sleep(3)
            await websocket.send(json.dumps({"type": "status", "state": "state-idle"}))
            self.voice.state = "IDLE"
            return

        print(f"=> TRANSCRIBED: {text}")

        # Capture local desktop context
        context = await perception.snapshot(text)
        context_data = {
            "active_app": context.active_app or "",
            "window_title": context.window_title or "",
            "browser_url": context.browser_url,
            "screen_text": context.screen_text,
        }

        # Send to Cloud Orchestrator
        if self.cloud_ws:
            await self.cloud_ws.send(json.dumps({
                "type": "transcription",
                "text": text,
                "context": context_data,
            }))
        else:
            print("[Mac] Not connected to Cloud! Cannot process request.")
            await websocket.send(json.dumps({
                "type": "response",
                "payload": {"text": "Cloud server is not connected.", "app": ""}
            }))
            await asyncio.sleep(3)
            await websocket.send(json.dumps({"type": "status", "state": "state-idle"}))

        self.voice.state = "IDLE"
        self.voice.audio_buffer = bytearray()
        self.voice.waiting_for_reply = False

    async def electron_handler(self, websocket):
        """Handle the Electron WebSocket connection locally."""
        print("[Mac] Electron App Connected!")
        self.electron_ws = websocket

        try:
            await websocket.send(json.dumps({"type": "status", "state": "state-idle"}))

            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.handle_electron_message(websocket, data)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    print(f"[Mac] Electron msg error: {e}")

        except websockets.exceptions.ConnectionClosed:
            print("[Mac] Electron disconnected")
        finally:
            self.electron_ws = None


async def main():
    """Start the Mac Client — connects to cloud AND hosts Electron WebSocket."""
    client = MacClient()

    # Start the cloud connection in the background
    cloud_task = asyncio.create_task(client.connect_to_cloud())

    # Host a local WebSocket for the Electron overlay (same as before)
    async with websockets.serve(
        client.electron_handler,
        "127.0.0.1",
        8000,
        origins=None,
    ):
        print(f"[Mac] Electron WebSocket server on ws://127.0.0.1:8000")
        print(f"[Mac] Connecting to Cloud at {CLOUD_URL}...")
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    asyncio.run(main())
