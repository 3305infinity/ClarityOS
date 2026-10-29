"""
Executable browser bridge/browser tool scenario tests.

Run directly with:
  python3 tests/test_browser_scenarios.py
"""

# pyright: reportMissingImports=false

import asyncio
import json
import os
import sys

import websockets

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

os.environ.setdefault("MOONWALK_BROWSER_BRIDGE_TOKEN", "dev-bridge-token")

from browser.bridge import browser_bridge
from browser.store import browser_store
from servers.browser_bridge_server import main_handler
import tools.browser_tools as browser_tools
from tools.browser_tools import (
    browser_assert,
    browser_click_match,
    browser_click_ref,
    browser_describe_ref,
    browser_find,
    browser_refresh_refs,
    browser_snapshot,
    browser_wait_for,
)


def _reset_state():
    browser_bridge.reset()
    browser_store.reset()


async def _with_server(test_coro):
    async with websockets.serve(main_handler, "127.0.0.1", 0, origins=None) as server:
        port = server.sockets[0].getsockname()[1]
        await test_coro(port)


async def test_no_snapshot_errors():
    _reset_state()
    result = await browser_snapshot()
    assert result.startswith("ERROR: No active browser snapshot")
    result = await browser_find("Continue")
    assert result.startswith("ERROR: No active browser snapshot")
    result = await browser_wait_for("Example")
    assert result.startswith("ERROR: No active browser snapshot")


async def test_invalid_token_handshake():
    _reset_state()

    async def scenario(port):
        async with websockets.connect(f"ws://127.0.0.1:{port}") as websocket:
            await websocket.send(json.dumps({
                "type": "browser_bridge_hello",
                "token": "bad-token",
                "session_id": "bad-session",
                "extension_name": "scenario-test",
            }))
            response = json.loads(await websocket.recv())
            assert response["type"] == "browser_bridge_hello_ack"
            assert response["ok"] is False
            assert browser_bridge.is_connected() is False

    await _with_server(scenario)


async def test_snapshot_and_tool_flow():
    _reset_state()

    async def scenario(port):
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps({
                "type": "browser_bridge_hello",
                "token": "dev-bridge-token",
                "session_id": "session-1",
                "extension_name": "scenario-test",
            }))
            ack = json.loads(await websocket.recv())
            assert ack["ok"] is True
            assert browser_bridge.is_connected() is True

            await websocket.send(json.dumps({
                "type": "browser_snapshot",
                "snapshot": {
                    "session_id": "session-1",
                    "tab_id": "tab-1",
                    "url": "https://checkout.example.com",
                    "title": "Checkout",
                    "generation": 7,
                    "elements": [
                        {
                            "ref_id": "mw_1",
                            "role": "button",
                            "tag": "button",
                            "text": "Continue",
                            "context_text": "Billing section",
                            "action_types": ["click"],
                            "visible": True,
                            "enabled": True,
                            "fingerprint": {
                                "role": "button",
                                "text": "Continue",
                                "ancestor_labels": ["Billing section"]
                            }
                        },
                        {
                            "ref_id": "mw_2",
                            "role": "button",
                            "tag": "button",
                            "text": "Continue",
                            "context_text": "Shipping section",
                            "action_types": ["click"],
                            "visible": True,
                            "enabled": True,
                            "fingerprint": {
                                "role": "button",
                                "text": "Continue",
                                "ancestor_labels": ["Shipping section"]
                            }
                        },
                        {
                            "ref_id": "mw_3",
                            "role": "textbox",
                            "tag": "input",
                            "placeholder": "Email",
                            "context_text": "Contact details",
                            "action_types": ["type"],
                            "visible": True,
                            "enabled": True,
                            "fingerprint": {
                                "role": "textbox",
                                "placeholder": "Email",
                                "ancestor_labels": ["Contact details"]
                            }
                        }
                    ]
                }
            }))
            snapshot_ack = json.loads(await websocket.recv())
            assert snapshot_ack["ok"] is True
            assert snapshot_ack["elements"] == 3

            summary = json.loads(await browser_snapshot("session-1"))
            assert summary["generation"] == 7
            assert summary["interactive_elements"] == 3
            assert "age_seconds" in summary

            fallback_summary = json.loads(await browser_snapshot("default_browser_session_id"))
            assert fallback_summary["generation"] == 7
            assert fallback_summary["session_id"] == "session-1"

            candidates = json.loads(await browser_find("Continue billing", action="click", session_id="session-1"))
            assert candidates["candidates"][0]["ref_id"] == "mw_1"
            assert candidates["best_candidate"]["ref_id"] == "mw_1"

            desc = json.loads(await browser_describe_ref("mw_3", session_id="session-1"))
            assert desc["label"] == "Email"
            assert desc["role"] == "textbox"

            queued = json.loads(await browser_click_ref("mw_1", session_id="session-1"))
            assert queued["ok"] is True
            assert queued["verification"]["success"] is True
            assert queued["action_id"].startswith("act_")

            matched_click = json.loads(await browser_click_match("Continue billing", session_id="session-1"))
            assert matched_click["selected_ref_id"] == "mw_1"
            assert matched_click["action"]["ok"] is True

            await websocket.send(json.dumps({
                "type": "browser_poll_actions",
                "session_id": "session-1",
            }))
            actions = json.loads(await websocket.recv())
            assert len(actions["actions"]) == 2
            assert all(action["action"] == "click" for action in actions["actions"])
            assert all(action["ref_id"] == "mw_1" for action in actions["actions"])
            assert {action["action_id"] for action in actions["actions"]} == {
                queued["action_id"],
                matched_click["action"]["action_id"],
            }
            assert all(action["metadata"]["tab_id"] == "tab-1" for action in actions["actions"])

            await websocket.send(json.dumps({
                "type": "browser_action_result",
                "result": {
                    "ok": True,
                    "message": "Clicked target button",
                    "action": "click",
                    "ref_id": "mw_1",
                    "action_id": queued["action_id"],
                    "session_id": "session-1",
                    "pre_generation": 7,
                    "post_generation": 8,
                    "details": {
                        "tab_id": "tab-1",
                        "executed_ref_id": "mw_1",
                    }
                }
            }))
            action_ack = json.loads(await websocket.recv())
            assert action_ack["type"] == "browser_action_result_ack"
            latest_result = browser_bridge.latest_action_result(queued["action_id"])
            assert latest_result is not None
            assert latest_result.ok is True
            assert latest_result.post_generation == 8

            await websocket.send(json.dumps({
                "type": "browser_action_result",
                "result": {
                    "ok": True,
                    "message": "Clicked target button again",
                    "action": "click",
                    "ref_id": "mw_1",
                    "action_id": matched_click["action"]["action_id"],
                    "session_id": "session-1",
                    "pre_generation": 7,
                    "post_generation": 8,
                    "details": {
                        "tab_id": "tab-1",
                        "executed_ref_id": "mw_1",
                    }
                }
            }))
            action_ack_2 = json.loads(await websocket.recv())
            assert action_ack_2["type"] == "browser_action_result_ack"

            await websocket.send(json.dumps({
                "type": "browser_poll_actions",
                "session_id": "session-1",
            }))
            actions_again = json.loads(await websocket.recv())
            assert actions_again["actions"] == []

            wait_ok = json.loads(await browser_wait_for("Checkout", session_id="session-1"))
            assert wait_ok["ok"] is True

            wait_missing = json.loads(await browser_assert("NotOnPage", session_id="session-1"))
            assert wait_missing["ok"] is False

            refresh_task = asyncio.create_task(browser_refresh_refs("session-1", timeout=0.5))

            await websocket.send(json.dumps({
                "type": "browser_poll_actions",
                "session_id": "session-1",
            }))
            refresh_actions = json.loads(await websocket.recv())
            assert len(refresh_actions["actions"]) == 1
            assert refresh_actions["actions"][0]["action"] == "refresh_snapshot"

            await websocket.send(json.dumps({
                "type": "browser_snapshot",
                "snapshot": {
                    "session_id": "session-1",
                    "tab_id": "tab-1",
                    "url": "https://checkout.example.com",
                    "title": "Checkout",
                    "generation": 9,
                    "elements": []
                }
            }))
            await websocket.recv()

            refresh = json.loads(await refresh_task)
            assert refresh["generation"] >= 9

    await _with_server(scenario)


async def test_unknown_ref_errors():
    _reset_state()

    async def scenario(port):
        async with websockets.connect(f"ws://127.0.0.1:{port}") as websocket:
            await websocket.send(json.dumps({
                "type": "browser_bridge_hello",
                "token": "dev-bridge-token",
                "session_id": "session-2",
                "extension_name": "scenario-test",
            }))
            await websocket.recv()
            await websocket.send(json.dumps({
                "type": "browser_snapshot",
                "snapshot": {
                    "session_id": "session-2",
                    "tab_id": "tab-2",
                    "url": "https://example.com",
                    "title": "Example",
                    "generation": 1,
                    "elements": []
                }
            }))
            await websocket.recv()

            payload = json.loads(await browser_describe_ref("missing-ref", session_id="session-2"))
            assert payload["ok"] is False
            assert payload["error_code"] == "unknown_ref"

    await _with_server(scenario)


async def test_browser_snapshot_waits_for_late_connection():
    _reset_state()

    async def scenario(port):
        async def delayed_snapshot_publish():
            await asyncio.sleep(0.2)
            uri = f"ws://127.0.0.1:{port}"
            async with websockets.connect(uri) as websocket:
                await websocket.send(json.dumps({
                    "type": "browser_bridge_hello",
                    "token": "dev-bridge-token",
                    "session_id": "late-session",
                    "extension_name": "scenario-test",
                }))
                await websocket.recv()
                await websocket.send(json.dumps({
                    "type": "browser_snapshot",
                    "snapshot": {
                        "session_id": "late-session",
                        "tab_id": "tab-late",
                        "url": "https://www.youtube.com",
                        "title": "YouTube",
                        "generation": 1,
                        "elements": []
                    }
                }))
                await websocket.recv()

        publisher = asyncio.create_task(delayed_snapshot_publish())
        summary = json.loads(await browser_snapshot("default_browser_session_id"))
        await publisher
        assert summary["session_id"] == "late-session"
        assert summary["title"] == "YouTube"

    await _with_server(scenario)


async def test_browser_click_match_prefers_youtube_video_links_for_generic_video_queries():
    _reset_state()
    original_selector = browser_tools.select_browser_candidate_with_flash

    async def fake_flash_select(query: str, action: str, session_id: str = "", text: str = "", option: str = "", limit: int = 8):
        snapshot = browser_store.get_snapshot(session_id)
        assert snapshot is not None
        return {
            "ref_id": "video_title_1",
            "reason": "Flash selected the first visible video result.",
            "model": "gemini-3-flash-preview",
            "degraded_mode": False,
            "degraded_reason": "",
            "candidates": [
                {
                    "ref_id": element.ref_id,
                    "label": element.primary_label(),
                    "role": element.role or element.tag,
                }
                for element in snapshot.elements
            ],
        }, ""

    browser_tools.select_browser_candidate_with_flash = fake_flash_select

    try:
        async def scenario(port):
            uri = f"ws://127.0.0.1:{port}"
            async with websockets.connect(uri) as websocket:
                await websocket.send(json.dumps({
                    "type": "browser_bridge_hello",
                    "token": "dev-bridge-token",
                    "session_id": "yt-session",
                    "extension_name": "scenario-test",
                }))
                hello = json.loads(await websocket.recv())
                assert hello["ok"] is True

                await websocket.send(json.dumps({
                    "type": "browser_snapshot",
                    "snapshot": {
                        "session_id": "yt-session",
                        "tab_id": "yt-tab",
                        "url": "https://www.youtube.com/results?search_query=japanese",
                        "title": "YouTube",
                        "generation": 10,
                        "elements": [
                            {
                                "ref_id": "nav_menu",
                                "role": "button",
                                "tag": "button",
                                "aria_label": "Guide",
                                "text": "",
                                "context_text": "Navigation",
                                "action_types": ["click"],
                                "visible": True,
                                "enabled": True,
                                "fingerprint": {
                                    "role": "button",
                                    "aria_label": "Guide",
                                    "ancestor_labels": ["Navigation"],
                                }
                            },
                            {
                                "ref_id": "video_title_1",
                                "role": "link",
                                "tag": "a",
                                "text": "Japanese Listening Practice For Beginners",
                                "href": "/watch?v=abc123",
                                "context_text": "Video result card",
                                "action_types": ["click"],
                                "visible": True,
                                "enabled": True,
                                "fingerprint": {
                                    "role": "link",
                                    "text": "Japanese Listening Practice For Beginners",
                                    "href": "/watch?v=abc123",
                                    "ancestor_labels": ["Video result card"],
                                }
                            },
                            {
                                "ref_id": "video_title_2",
                                "role": "link",
                                "tag": "a",
                                "text": "JLPT N5 Shadowing Exercise",
                                "href": "/watch?v=def456",
                                "context_text": "Video result card",
                                "action_types": ["click"],
                                "visible": True,
                                "enabled": True,
                                "fingerprint": {
                                    "role": "link",
                                    "text": "JLPT N5 Shadowing Exercise",
                                    "href": "/watch?v=def456",
                                    "ancestor_labels": ["Video result card"],
                                }
                            }
                        ]
                    }
                }))
                snapshot_ack = json.loads(await websocket.recv())
                assert snapshot_ack["ok"] is True

                matched_click = json.loads(await browser_click_match("video thumbnail or title", session_id="yt-session"))
                assert matched_click["selected_ref_id"] == "video_title_1"
                assert matched_click["selection_model"] == "gemini-3-flash-preview"
                assert matched_click["degraded_mode"] is False
                assert matched_click["action"]["ok"] is True

                await websocket.send(json.dumps({
                    "type": "browser_poll_actions",
                    "session_id": "yt-session",
                }))
                actions = json.loads(await websocket.recv())
                assert len(actions["actions"]) == 1
                assert actions["actions"][0]["ref_id"] == "video_title_1"

        await _with_server(scenario)
    finally:
        browser_tools.select_browser_candidate_with_flash = original_selector


async def main():
    await test_no_snapshot_errors()
    print("✓ no-snapshot error handling")
    await test_invalid_token_handshake()
    print("✓ invalid-token handshake")
    await test_snapshot_and_tool_flow()
    print("✓ snapshot/tool/queue flow")
    await test_unknown_ref_errors()
    print("✓ unknown-ref errors")
    await test_browser_snapshot_waits_for_late_connection()
    print("✓ late browser snapshot recovery")
    await test_browser_click_match_prefers_youtube_video_links_for_generic_video_queries()
    print("✓ generic YouTube video clicks prefer video links")
    print("All current browser scenarios passed")


if __name__ == "__main__":
    asyncio.run(main())
