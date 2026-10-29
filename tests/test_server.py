import asyncio
import websockets
import json

async def handler(websocket):
    print("Electron connected!")
    await websocket.send(json.dumps({"state": "IDLE"}))
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "audio_chunk":
                    payload = data.get("payload", "")
                    print(f"Received audio chunk: {payload[:20]}... (length: {len(payload)})")
            except json.JSONDecodeError:
                print("Received non-JSON message")
    except websockets.exceptions.ConnectionClosed:
        print("Electron disconnected!")

async def main():
    async with websockets.serve(handler, "localhost", 8000):
        print("Test WebSocket server running on ws://localhost:8000")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
