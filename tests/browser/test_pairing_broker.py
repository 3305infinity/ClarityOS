"""
Approval-gated pairing: the broker must never release the code without a human.

Run: backend/.venv/Scripts/python.exe -m pytest tests/browser/test_pairing_broker.py -q
"""
import asyncio
import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from browser.pairing import PairingBroker  # noqa: E402

SECRET = "pairing-code-under-test"


def _broker(notify=None):
    broker = PairingBroker()
    seen = []

    async def _notify(event):
        seen.append(event)
        if notify:
            await notify(broker, event)

    broker.configure(_notify, lambda: SECRET)
    return broker, seen


async def test_unconfigured_broker_refuses():
    broker = PairingBroker()
    approved, token, message = await broker.request("Ext", "chrome-extension://abc")
    assert approved is False
    assert token == ""
    assert "options" in message.lower()


async def test_approval_releases_the_code():
    async def approve(broker, event):
        # Simulate the user clicking Approve in the hub window.
        asyncio.get_running_loop().call_soon(broker.resolve, event["request_id"], True)

    broker, seen = _broker(approve)
    approved, token, message = await broker.request("ClarityOS companion", "chrome-extension://abc")
    assert approved is True
    assert token == SECRET
    assert message == ""
    assert seen[0]["extension_name"] == "ClarityOS companion"
    assert "token" not in seen[0], "the prompt payload must not carry the code"


async def test_denial_releases_nothing():
    async def deny(broker, event):
        asyncio.get_running_loop().call_soon(broker.resolve, event["request_id"], False)

    broker, _ = _broker(deny)
    approved, token, message = await broker.request("Ext", "chrome-extension://abc")
    assert approved is False
    assert token == ""
    assert "denied" in message.lower()


async def test_timeout_is_a_denial(monkeypatch):
    import browser.pairing as pairing

    monkeypatch.setattr(pairing, "PAIR_TIMEOUT_S", 0.2)
    broker, _ = _broker()          # nobody ever answers
    approved, token, message = await broker.request("Ext", "chrome-extension://abc")
    assert approved is False
    assert token == ""
    assert "expired" in message.lower()


async def test_unknown_request_id_cannot_approve():
    async def wrong_id(broker, event):
        assert broker.resolve("not-a-real-id", True) is False
        asyncio.get_running_loop().call_soon(broker.resolve, event["request_id"], False)

    broker, _ = _broker(wrong_id)
    approved, _, _ = await broker.request("Ext", "chrome-extension://abc")
    assert approved is False


async def test_pending_requests_are_capped():
    import browser.pairing as pairing

    broker, _ = _broker()          # nothing resolves, so requests pile up
    tasks = [asyncio.create_task(broker.request("Ext", "o")) for _ in range(pairing.MAX_PENDING)]
    await asyncio.sleep(0.05)
    approved, token, message = await broker.request("Flooder", "o")
    assert approved is False
    assert "too many" in message.lower()
    broker.cancel_all()
    for task in tasks:
        task.cancel()


async def test_notify_failure_does_not_strand_the_request():
    broker = PairingBroker()

    async def _broken(event):
        raise RuntimeError("no hub ui connected")

    broker.configure(_broken, lambda: SECRET)
    approved, token, message = await broker.request("Ext", "o")
    assert approved is False
    assert token == ""
    assert "could not reach" in message.lower()
    assert broker.pending() == [], "a failed prompt must not leave a pending request"
