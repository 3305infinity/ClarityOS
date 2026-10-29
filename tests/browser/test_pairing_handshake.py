"""
End-to-end pairing over the real bridge WebSocket server.

Starts the actual `bridge_handler`, connects a real websocket client, and walks
the real protocol: browser_pair_request -> approval -> browser_bridge_hello.

Run: backend/.venv/Scripts/python.exe -m pytest tests/browser/test_pairing_handshake.py -q
"""
import asyncio
import json
import os
import sys

import pytest
import websockets
from websockets.asyncio.server import serve

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from browser.bridge import browser_bridge  # noqa: E402
from browser.pairing import pairing_broker  # noqa: E402
from servers.browser_bridge_server import bridge_handler  # noqa: E402

SECRET = "e2e-pairing-code"


@pytest.fixture
async def bridge():
    browser_bridge._session_token = SECRET
    browser_bridge.disconnect()
    pairing_broker.cancel_all()
    async with serve(bridge_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    pairing_broker._notify = None
    pairing_broker._token_provider = None


def _arm(decision: bool):
    """Wire the broker to a fake hub UI that answers with `decision`."""
    prompts = []

    async def notify(event):
        prompts.append(event)
        asyncio.get_running_loop().call_soon(pairing_broker.resolve, event["request_id"], decision)

    pairing_broker.configure(notify, lambda: SECRET)
    return prompts


async def test_unpaired_extension_can_pair_after_approval(bridge):
    prompts = _arm(True)
    async with websockets.connect(bridge) as ws:
        await ws.send(json.dumps({"type": "browser_pair_request", "extension_name": "ClarityOS companion"}))
        result = json.loads(await asyncio.wait_for(ws.recv(), 10))

        assert result["type"] == "browser_pair_result"
        assert result["ok"] is True
        assert result["token"] == SECRET
        assert prompts and prompts[0]["extension_name"] == "ClarityOS companion"

        # The granted code must then authenticate through the unchanged handshake.
        await ws.send(json.dumps({
            "type": "browser_bridge_hello",
            "token": result["token"],
            "session_id": "sess-1",
            "extension_name": "ClarityOS companion",
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert ack["type"] == "browser_bridge_hello_ack"
        assert ack["ok"] is True
        assert browser_bridge.is_connected()


async def test_denied_pairing_yields_no_token_and_no_access(bridge):
    _arm(False)
    async with websockets.connect(bridge) as ws:
        await ws.send(json.dumps({"type": "browser_pair_request", "extension_name": "Sketchy ext"}))
        result = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert result["ok"] is False
        assert result["token"] == ""

        # Still unauthenticated: privileged messages must be refused.
        await ws.send(json.dumps({"type": "browser_poll_actions"}))
        refusal = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert refusal["type"] == "error"
        assert "Authenticate first" in refusal["message"]


async def test_wrong_token_still_rejected_after_pairing_exists(bridge):
    _arm(True)
    async with websockets.connect(bridge) as ws:
        await ws.send(json.dumps({
            "type": "browser_bridge_hello",
            "token": "not-the-code",
            "session_id": "sess-2",
            "extension_name": "Ext",
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert ack["ok"] is False
        assert ack["message"] == "Invalid bridge token"


async def test_pair_request_refused_once_authenticated(bridge):
    _arm(True)
    async with websockets.connect(bridge) as ws:
        await ws.send(json.dumps({
            "type": "browser_bridge_hello",
            "token": SECRET,
            "session_id": "sess-3",
            "extension_name": "Ext",
        }))
        assert json.loads(await asyncio.wait_for(ws.recv(), 10))["ok"] is True

        await ws.send(json.dumps({"type": "browser_pair_request", "extension_name": "Ext"}))
        result = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert result["ok"] is False
        assert result["token"] == ""


async def test_pairing_is_unavailable_without_a_hub_ui(bridge):
    # No broker configuration == no window to approve in. Must refuse, not grant.
    pairing_broker._notify = None
    pairing_broker._token_provider = None
    async with websockets.connect(bridge) as ws:
        await ws.send(json.dumps({"type": "browser_pair_request", "extension_name": "Ext"}))
        result = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert result["ok"] is False
        assert result["token"] == ""
