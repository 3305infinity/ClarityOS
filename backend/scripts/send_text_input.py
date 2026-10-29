"""Send a quick text command to the local Moonwalk server over WebSocket."""

import asyncio
import json
import sys

import websockets


async def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print("Usage: python backend/scripts/send_text_input.py <command text>")
        return 1

    uri = "ws://127.0.0.1:8000"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "type": "text_input",
            "text": text,
            "context": {},
        }))

        try:
            while True:
                message = await asyncio.wait_for(websocket.recv(), timeout=15)
                print(message)
        except asyncio.TimeoutError:
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))