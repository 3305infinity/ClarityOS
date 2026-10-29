"""
ClarityOS — policy guard for the Electron voice path
===================================================
The hub (website) wraps `registry.execute` with `Hub.install_guard`, so every
tool call from that front-end passes the PolicyEngine first. The Electron voice
server never installed that guard, which meant a spoken "call this number"
reached `bolna_call` — real money, a real person — with no approval step. The
only thing standing in the way was a sentence in the tool description asking the
model to confirm, and a model is not an access-control mechanism.

This module closes that gap using the *same* `PolicyEngine`, the *same*
`ApprovalBroker` and the *same* WebSocket event names as the hub, so there is one
policy implementation and one approval implementation for both front-ends.

It deliberately does NOT reimplement the hub's extras (task store, pause/resume,
CAPTCHA notes, event persistence) — those belong to the hub's task model. This
guard is only the security choke point.

Settings come from the same `settings.json` the hub writes, so the permission
switches in the website's Settings → Permissions apply to the voice path too.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any, Awaitable, Callable, Dict, Optional

from hub import config as cfg
from hub.approval import APPROVAL_TIMEOUT_S, ApprovalBroker
from hub.policy import PolicyEngine
from hub.redact import make_redactor

print = partial(print, flush=True)

__all__ = ["VoiceToolGuard", "install_voice_guard", "get_voice_guard"]

_DENIED_BY_USER = ("The user did not approve this action. Do not retry it; "
                   "ask what they prefer.")


def _configured_secret_values() -> list:
    """
    Current API-key values, so an approval summary can never echo one back to the
    UI. Read from the environment (where the hub's SecretStore puts them) rather
    than from the keyring, to keep this import-light. Values are never logged.
    """
    import os
    out = []
    for name in cfg.SECRET_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value and len(value) >= 8:
            out.append(value)
    return out


class VoiceToolGuard:
    """
    Wraps `registry.execute` for the Electron voice path.

    One guard per process (the tool registry is a module-level singleton). The
    attached WebSocket changes per connection, so `attach()`/`detach()` move the
    approval UI between connections without reinstalling the wrapper.
    """

    def __init__(self, settings_getter: Optional[Callable[[], Dict[str, Any]]] = None,
                 redactor: Optional[Callable[[Any], str]] = None,
                 timeout_s: float = APPROVAL_TIMEOUT_S):
        if settings_getter is None:
            settings = cfg.Settings(cfg.user_dir() / "settings.json")
            settings_getter = settings.get
            self._settings = settings
        else:
            self._settings = None
        self.policy = PolicyEngine(settings_getter)
        self.approvals = ApprovalBroker(timeout_s)
        self.redact = redactor or make_redactor(_configured_secret_values)
        self._ws: Any = None
        self._installed_on: Any = None

    # ── connection plumbing ──────────────────────────────────────

    def attach(self, websocket: Any) -> None:
        """Point approval prompts at this connection."""
        self._ws = websocket

    def detach(self, websocket: Any = None) -> None:
        """
        Drop the connection and deny anything still pending.

        A disconnect must never leave an approved-looking action behind: if the
        window closed, nobody is there to approve.
        """
        if websocket is not None and self._ws is not websocket:
            return
        self._ws = None
        self.approvals.cancel_all("ui disconnected")

    def cancel_pending(self, reason: str = "user cancelled") -> int:
        """Deny every pending approval — used by cancel_task and shutdown."""
        return self.approvals.cancel_all(reason)

    def resolve(self, approval_id: str, approve: bool) -> bool:
        """Settle one approval. False means the id was unknown, stale or replayed."""
        return self.approvals.resolve(approval_id, approve)

    async def _emit(self, event: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("no UI attached")
        await ws.send(json.dumps(event))

    # ── the guard itself ─────────────────────────────────────────

    def install(self, registry: Any) -> None:
        """
        Wrap `registry.execute`. Idempotent: always wraps the ORIGINAL execute, so
        reinstalling (reconnect, reload, tests) never stacks guards.
        """
        orig = getattr(registry, "_mw_voice_orig_execute", None) or registry.execute
        registry._mw_voice_orig_execute = orig
        guard = self

        async def guarded(name: str, args: Dict[str, Any]) -> str:
            return await guard._run(orig, name, args)

        registry.execute = guarded
        self._installed_on = registry
        print("[VoiceGuard] policy guard installed on the tool registry")

    async def _run(self, orig: Callable, name: str, args: Dict[str, Any]) -> str:
        from tools.contracts import error_envelope

        decision = self.policy.check(name, args or {})

        if decision.action == "deny":
            print(f"[VoiceGuard] denied '{name}' — {decision.reason}")
            await self._notify({"type": "tool_blocked", "tool": name,
                                "capability": decision.capability, "reason": decision.reason})
            return str(error_envelope(code="policy.denied",
                                      message=f"Blocked by user policy: {decision.reason}"))

        if decision.action == "ask":
            approved = await self.approvals.request(
                tool=name,
                capability=decision.capability,
                reason=decision.reason,
                summary=str(self.redact(decision.summary))[:500],
                emit=self._emit,
            )
            if not approved:
                print(f"[VoiceGuard] '{name}' was not approved")
                return str(error_envelope(code="policy.user_denied", message=_DENIED_BY_USER))

        return await orig(name, args)

    async def _notify(self, event: Dict[str, Any]) -> None:
        """Best-effort UI event. Never changes the security decision."""
        try:
            await self._emit(event)
        except Exception:
            pass


# ── process-wide singleton ───────────────────────────────────────

_guard: Optional[VoiceToolGuard] = None


def install_voice_guard(registry: Any, **kwargs: Any) -> VoiceToolGuard:
    """Create (once) and install the voice-path guard."""
    global _guard
    if _guard is None:
        _guard = VoiceToolGuard(**kwargs)
    _guard.install(registry)
    return _guard


def get_voice_guard() -> Optional[VoiceToolGuard]:
    return _guard
