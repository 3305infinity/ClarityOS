"""Trigger a Gemini 3 Flash-driven browser extension action through the local Moonwalk server."""

import argparse
import asyncio
import json

import websockets


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Human query for the browser element, e.g. 'main search box'")
    parser.add_argument("--action", default="click", choices=["click", "type", "select"])
    parser.add_argument("--text", default="")
    parser.add_argument("--option", default="")
    parser.add_argument("--clear-first", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    async with websockets.connect("ws://127.0.0.1:8000") as websocket:
        await websocket.recv()
        await websocket.send(json.dumps({
            "type": "browser_flash_action",
            "query": args.query,
            "action": args.action,
            "text": args.text,
            "option": args.option,
            "clear_first": args.clear_first,
            "timeout": args.timeout,
        }))

        while True:
            message = json.loads(await websocket.recv())
            if message.get("type") == "browser_flash_result":
                print(json.dumps(message, ensure_ascii=False, indent=2))
                return 0 if message.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))