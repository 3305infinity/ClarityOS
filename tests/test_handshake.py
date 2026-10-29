import sys
import asyncio
import websockets

async def handler(websocket):
    print("HANDSHAKE SUCCESSFUL", flush=True)

async def main():
    async with websockets.serve(handler, "localhost", 8000, ping_interval=None):
        print("Listening on 8000", flush=True)
        await asyncio.Future()

asyncio.run(main())
