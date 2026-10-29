"""
ClarityOS — Browser Bridge Pairing
==================================
Approval-gated pairing for the Chrome extension.

Why this exists
---------------
Pairing used to mean: open the hub, go to Settings -> Connections, copy a
secret, open chrome://extensions, find Details -> Extension options, paste,
save. Six steps, and the secret travelled through the clipboard.

Fully automatic pairing is *not* safe: the bridge listens on loopback, so any
local process — including a hostile one — could ask for the code. So one
explicit human approval is kept. The extension asks, the hub shows who is
asking, and only a click hands the code back. Deny-by-default, single use,
time-limited.

Nothing here ever logs the pairing code.
"""

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# How long a pairing request waits for a human before it expires.
PAIR_TIMEOUT_S: float = 120.0
# Stops a local process from flooding the hub UI with prompts.
MAX_PENDING: int = 3


@dataclass
class PairingRequest:
    request_id: str
    extension_name: str
    origin: str
    created_at: float
    future: "asyncio.Future[bool]"

    def to_event(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "extension_name": self.extension_name,
            "origin": self.origin,
            "timeout": PAIR_TIMEOUT_S,
        }


class PairingBroker:
    """
    Deny-by-default broker between the bridge server and the hub UI.

    The bridge server calls :meth:`request` and awaits. The hub UI is notified,
    and :meth:`resolve` is called when the user clicks Approve or Deny. No
    approval, no code — a timeout resolves as a denial.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, PairingRequest] = {}
        self._notify: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self._token_provider: Optional[Callable[[], str]] = None

    def configure(
        self,
        notify: Callable[[Dict[str, Any]], Awaitable[None]],
        token_provider: Callable[[], str],
    ) -> None:
        """Wire the broker to a hub that can show prompts and mint the code."""
        self._notify = notify
        self._token_provider = token_provider

    @property
    def enabled(self) -> bool:
        return self._notify is not None and self._token_provider is not None

    def pending(self) -> List[Dict[str, Any]]:
        return [req.to_event() for req in self._pending.values()]

    async def request(self, extension_name: str, origin: str) -> Tuple[bool, str, str]:
        """
        Ask the user to approve one extension.

        Returns ``(approved, token, message)``. ``token`` is only non-empty when
        ``approved`` is True.
        """
        if not self.enabled:
            # The standalone bridge server has no UI to ask through. Pairing
            # stays manual there rather than silently trusting the caller.
            return False, "", "This backend cannot show a pairing prompt. Paste the code in the extension's options instead."

        if len(self._pending) >= MAX_PENDING:
            return False, "", "Too many pairing requests are already waiting for approval."

        request_id = secrets.token_urlsafe(12)
        loop = asyncio.get_running_loop()
        record = PairingRequest(
            request_id=request_id,
            extension_name=extension_name or "Chrome extension",
            origin=origin or "unknown origin",
            created_at=time.time(),
            future=loop.create_future(),
        )
        self._pending[request_id] = record

        try:
            await self._notify(record.to_event())
        except Exception as exc:  # hub UI not connected / send failed
            self._pending.pop(request_id, None)
            return False, "", f"Could not reach the ClarityOS window to ask for approval ({exc.__class__.__name__})."

        try:
            approved = await asyncio.wait_for(record.future, PAIR_TIMEOUT_S)
        except asyncio.TimeoutError:
            return False, "", "Pairing request expired — nobody approved it in the ClarityOS window."
        finally:
            self._pending.pop(request_id, None)

        if not approved:
            return False, "", "Pairing request was denied in ClarityOS."

        token = (self._token_provider() or "").strip()
        if not token:
            return False, "", "ClarityOS has no pairing code configured."
        return True, token, ""

    def resolve(self, request_id: str, approved: bool) -> bool:
        """Settle a pending request. Returns False for unknown/stale ids."""
        record = self._pending.get(request_id)
        if record is None or record.future.done():
            return False
        record.future.set_result(bool(approved))
        return True

    def cancel_all(self, reason: str = "cancelled") -> None:
        for record in list(self._pending.values()):
            if not record.future.done():
                record.future.set_result(False)
        self._pending.clear()


pairing_broker = PairingBroker()
