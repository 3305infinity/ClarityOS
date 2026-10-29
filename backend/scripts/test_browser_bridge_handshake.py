"""
Simple browser bridge handshake test client.
"""

import asyncio
import json
import os
import sys

import websockets

HOST = os.environ.get("MOONWALK_BROWSER_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MOONWALK_BROWSER_BRIDGE_PORT", "8765"))
TOKEN = os.environ.get("MOONWALK_BROWSER_BRIDGE_TOKEN", "dev-bridge-token")


async def main():
    uri = f"ws://{HOST}:{PORT}"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "type": "browser_bridge_hello",
            "token": TOKEN,
            "session_id": "test-session-1",
            "extension_name": "handshake-test-client",
        }))
        print(await websocket.recv())

        await websocket.send(json.dumps({
            "type": "browser_ping"
        }))
        print(await websocket.recv())

        await websocket.send(json.dumps({
            "type": "browser_snapshot",
            "snapshot": {
                "session_id": "test-session-1",
                "tab_id": "tab-1",
                "url": "https://example.com",
                "title": "Example Domain",
                "generation": 1,
                "elements": [
                    {
                        "ref_id": "link_more_info",
                        "role": "link",
                        "tag": "a",
                        "text": "More information...",
                        "href": "https://www.iana.org/help/example-domains",
                        "context_text": "Example Domain hero",
                        "action_types": ["click"],
                        "visible": True,
                        "enabled": True,
                        "fingerprint": {
                            "role": "link",
                            "text": "More information...",
                            "ancestor_labels": ["Example Domain"]
                        }
                    }
                ]
            }
        }))
        print(await websocket.recv())

        await websocket.send(json.dumps({
            "type": "browser_poll_actions",
            "session_id": "test-session-1"
        }))
        print(await websocket.recv())


if __name__ == "__main__":
    asyncio.run(main())
