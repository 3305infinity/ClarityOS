"""
Moonwalk — daily-quota bookkeeping
==================================
Process-wide record of which Gemini models have spent their daily allowance.

Why this exists: FAST, POWERFUL and FALLBACK are often configured to the same
model (and public ids alias onto a shared quota bucket). Without this, one
exhausted model is retried once per tier per request — four guaranteed-failing
API calls that each cost latency and tell the user nothing useful.

Entries are keyed on the *billed* model name reported in the quota violation, so
`gemini-3.5-flash` and `gemini-3.6-flash` are recognised as the same bucket.
State is in-memory only: restarting the hub clears it, and the next real API
call re-learns the truth from Google.
"""

from __future__ import annotations

import threading
import time
from functools import partial
from typing import Dict, Optional

from providers.errors import ProviderError, ProviderErrorKind

print = partial(print, flush=True)

__all__ = [
    "mark_quota_exhausted",
    "quota_exhausted_until",
    "is_quota_exhausted",
    "exhausted_models",
    "clear_quota_state",
]

# Fallback when Google sends no RetryInfo: free-tier daily buckets reset at
# midnight US/Pacific, so an hour is a safe, self-correcting guess.
_DEFAULT_COOLDOWN_S = 3600.0

_lock = threading.Lock()
_exhausted: Dict[str, float] = {}      # model name -> epoch seconds when it may work again


def _aliases(primary: str, *others: str) -> list:
    return [m.strip().lower() for m in (primary, *others) if m and m.strip()]


def mark_quota_exhausted(failure: ProviderError, served_model: str = "") -> None:
    """Record a daily-quota failure. Ignores anything that is not daily exhaustion."""
    if failure.kind is not ProviderErrorKind.QUOTA_DAILY:
        return
    until = time.time() + (failure.retry_after_s or _DEFAULT_COOLDOWN_S)
    names = _aliases(failure.exhausted_model, failure.requested_model, served_model)
    if not names:
        return
    with _lock:
        for name in names:
            if _exhausted.get(name, 0.0) < until:
                _exhausted[name] = until
    print(f"[Quota] {', '.join(sorted(set(names)))} exhausted for "
          f"{failure.retry_hint() or 'a while'} — skipping until it resets")


def quota_exhausted_until(model: str) -> Optional[float]:
    """Epoch seconds until `model` is usable again, or None if it is usable now."""
    if not model:
        return None
    key = model.strip().lower()
    now = time.time()
    with _lock:
        until = _exhausted.get(key)
        if until is None:
            return None
        if until <= now:
            del _exhausted[key]          # cooldown elapsed; let the next call find out
            return None
        return until


def is_quota_exhausted(model: str) -> bool:
    return quota_exhausted_until(model) is not None


def exhausted_models() -> Dict[str, float]:
    """Snapshot of currently-exhausted models, for diagnostics."""
    now = time.time()
    with _lock:
        return {m: u for m, u in _exhausted.items() if u > now}


def clear_quota_state() -> None:
    """Reset everything. Used by tests and by an explicit user retry."""
    with _lock:
        _exhausted.clear()
