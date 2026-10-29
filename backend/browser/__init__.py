"""
Moonwalk browser automation contract layer.

This package owns stable browser element identities, snapshot state,
candidate resolution, and extension bridge state.
"""

from .models import (
    PageSnapshot,
    ElementRef,
    ElementFingerprint,
    ActionRequest,
    ActionResult,
    VerificationReport,
)
from .store import browser_store
from .resolver import BrowserResolver
from .bridge import browser_bridge

__all__ = [
    "PageSnapshot",
    "ElementRef",
    "ElementFingerprint",
    "ActionRequest",
    "ActionResult",
    "VerificationReport",
    "browser_store",
    "BrowserResolver",
    "browser_bridge",
]
