"""
ClarityOS — model configuration (single source of truth)
=======================================================
Every Gemini model id used anywhere in the backend is resolved here.

Precedence, highest first:
    1. environment variable  (GEMINI_FAST_MODEL, GEMINI_POWERFUL_MODEL,
                              GEMINI_ROUTING_MODEL, GEMINI_FALLBACK_MODEL)
       — set in backend/.env, by the hub's secret store, or by the UI
    2. the defaults below

Before this module the same defaults were spelled out separately in
providers/router.py, hub/desktop_proxy.py, tools/mac_tools.py and
browser/interpreter_ai.py, so changing one left the others behind. Import from
here instead of writing `os.environ.get("GEMINI_FAST_MODEL", ...)` inline.

Aliases vs billed models
------------------------
The ids below are public aliases. Google serves them from a dated family member
and bills that member's quota, so an API error may name a model you never
configured. Observed on 2026-10-26:

    requested gemini-3.5-flash  ->  billed gemini-3.6-flash

This is expected, not a misconfiguration. `GeminiProvider.served_model` reports
the resolved name once a call succeeds, and quota bookkeeping keys on the billed
name so all aliases of one family are treated as a single bucket.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_ROUTING_MODEL",
    "DEFAULT_FAST_MODEL",
    "DEFAULT_POWERFUL_MODEL",
    "DEFAULT_FALLBACK_MODEL",
    "routing_model",
    "fast_model",
    "powerful_model",
    "fallback_model",
    "configured_models",
]

# Defaults. Free-tier-friendly: all four tiers point at one widely-available
# model. Set GEMINI_POWERFUL_MODEL to a Pro id in backend/.env to restore real
# tiering once the project has billing enabled.
DEFAULT_ROUTING_MODEL = "gemini-3.5-flash"
DEFAULT_FAST_MODEL = "gemini-3.5-flash"
DEFAULT_POWERFUL_MODEL = "gemini-3.5-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash"


def _env_model(name: str, default: str) -> str:
    """Read a model override, ignoring blank/whitespace values."""
    return (os.environ.get(name, "") or "").strip() or default


def routing_model() -> str:
    return _env_model("GEMINI_ROUTING_MODEL", DEFAULT_ROUTING_MODEL)


def fast_model() -> str:
    return _env_model("GEMINI_FAST_MODEL", DEFAULT_FAST_MODEL)


def powerful_model() -> str:
    return _env_model("GEMINI_POWERFUL_MODEL", DEFAULT_POWERFUL_MODEL)


def fallback_model() -> str:
    return _env_model("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)


def configured_models() -> dict:
    """Tier -> model id, as currently resolved. For startup logs and diagnostics."""
    return {
        "routing": routing_model(),
        "fast": fast_model(),
        "powerful": powerful_model(),
        "fallback": fallback_model(),
    }
