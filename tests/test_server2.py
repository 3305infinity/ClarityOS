import asyncio
import websockets

async def handler(websocket):
    print("Connected!")
    async for message in websocket:
        print(f"Received msg length: {len(message)}")

async def main():
    async with websockets.serve(handler, "localhost", 8001):
        print("Listening on 8001")
        await asyncio.Future()

asyncio.run(main())
