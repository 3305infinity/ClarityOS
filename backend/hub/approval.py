"""
Moonwalk — approval broker
==========================
The one place that decides whether a pending consequential action was actually
approved by a human.

Both front-ends use it: the hub (website) via `Hub.request_approval`, and the
Electron voice path via `servers.voice_guard`. Keeping a single implementation
matters because every property below is a security property — duplicating the
logic for a second transport is exactly how one copy drifts and starts
defaulting to "approved".

Guarantees
----------
* **Deny by default.** Nothing but an explicit `resolve(id, True)` from the user
  returns True. Timeout, cancellation, shutdown, a dropped socket, an unknown id
  and an internal error all return False.
* **Unguessable, single-use ids.** Each request mints a fresh
  `secrets.token_urlsafe` nonce and removes it once settled, so an approval
  cannot be replayed or reused for a different action.
* **Bound to one action.** The id is minted inside `request()` for one specific
  tool call; `resolve()` only ever completes that call's future.
* **Bounded lifetime.** Every request expires after `timeout_s`.

The model never sees an approval id and cannot mint or resolve one — only a real
UI message carrying a live id can.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Awaitable, Callable, Dict, List, Optional

print = partial(print, flush=True)

__all__ = ["APPROVAL_TIMEOUT_S", "PendingApproval", "ApprovalBroker"]

APPROVAL_TIMEOUT_S = 120.0

# Async callable that delivers an event dict to whichever UI is attached.
Emit = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class PendingApproval:
    """One in-flight approval request."""
    approval_id: str
    tool: str
    capability: str = ""
    reason: str = ""
    summary: str = ""
    task_id: str = ""
    created_at: float = field(default_factory=time.time)
    future: Optional[asyncio.Future] = field(default=None, repr=False)

    def to_event(self, timeout_s: float) -> Dict[str, Any]:
        """The `approval_request` payload. Shape is shared by both front-ends."""
        return {
            "type": "approval_request",
            "task_id": self.task_id,
            "approval_id": self.approval_id,
            "tool": self.tool,
            "capability": self.capability,
            "reason": self.reason,
            "summary": self.summary,
            "timeout": timeout_s,
        }


class ApprovalBroker:
    """Mints, tracks and settles approval requests. Safe to share across tasks."""

    def __init__(self, timeout_s: float = APPROVAL_TIMEOUT_S):
        self.timeout_s = float(timeout_s)
        self._pending: Dict[str, PendingApproval] = {}

    # ── lifecycle ────────────────────────────────────────────────

    async def request(
        self,
        *,
        tool: str,
        capability: str = "",
        reason: str = "",
        summary: str = "",
        task_id: str = "",
        emit: Optional[Emit] = None,
    ) -> bool:
        """
        Ask the user to approve one tool call. Returns True only on explicit approval.

        `emit` delivers the prompt to the UI. If it raises — socket closed, no UI
        attached — we deny rather than silently proceeding: an action nobody could
        see is an action nobody approved.
        """
        approval_id = secrets.token_urlsafe(16)
        loop = asyncio.get_running_loop()
        pending = PendingApproval(
            approval_id=approval_id,
            tool=tool,
            capability=capability,
            reason=reason,
            summary=summary,
            task_id=task_id,
            future=loop.create_future(),
        )
        self._pending[approval_id] = pending

        try:
            if emit is None:
                print(f"[Approval] no UI attached — denying '{tool}'")
                return False
            try:
                await emit(pending.to_event(self.timeout_s))
            except Exception as e:
                print(f"[Approval] could not reach the UI ({e.__class__.__name__}) — denying '{tool}'")
                return False

            try:
                return bool(await asyncio.wait_for(pending.future, self.timeout_s))
            except asyncio.TimeoutError:
                print(f"[Approval] '{tool}' expired after {self.timeout_s:.0f}s — denying")
                return False
            except asyncio.CancelledError:
                # The task was cancelled while waiting; the action must not run.
                print(f"[Approval] '{tool}' cancelled while awaiting approval — denying")
                raise
        finally:
            # Single-use: the id stops being resolvable the moment it settles.
            self._pending.pop(approval_id, None)
            if emit is not None:
                fut = pending.future
                approved = bool(fut is not None and fut.done() and not fut.cancelled()
                                and fut.exception() is None and fut.result() is True)
                try:
                    await emit({
                        "type": "approval_resolved",
                        "task_id": task_id,
                        "approval_id": approval_id,
                        "approved": approved,
                    })
                except Exception:
                    pass        # the decision already stands; telling the UI is best-effort

    def resolve(self, approval_id: str, approve: bool) -> bool:
        """
        Settle a pending request. Returns False for unknown, replayed or already
        settled ids — the caller should treat that as "nothing was approved".
        """
        pending = self._pending.get(str(approval_id or ""))
        if pending is None or pending.future is None or pending.future.done():
            return False
        pending.future.set_result(bool(approve))
        return True

    def cancel_all(self, reason: str = "cancelled") -> int:
        """Deny every pending request. Used on cancel, disconnect and shutdown."""
        count = 0
        for pending in list(self._pending.values()):
            if pending.future is not None and not pending.future.done():
                pending.future.set_result(False)
                count += 1
        if count:
            print(f"[Approval] denied {count} pending request(s) — {reason}")
        return count

    # ── introspection ────────────────────────────────────────────

    def pending(self) -> List[PendingApproval]:
        return list(self._pending.values())

    def has_pending(self) -> bool:
        return bool(self._pending)
