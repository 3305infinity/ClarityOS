"""
Moonwalk — Browser Verification Helpers
=======================================
Basic verification utilities for browser ref actions.
"""

from .models import ActionResult, VerificationReport
from .store import browser_store


def verify_action_result(result: ActionResult) -> VerificationReport:
    snapshot = browser_store.get_snapshot(result.session_id) if result.session_id else None
    checks = []

    if result.ok:
        checks.append("action_queued")
    if snapshot:
        checks.append("snapshot_present")

    return VerificationReport(
        success=result.ok,
        confidence=0.8 if result.ok else 0.2,
        message=result.message,
        checks_passed=checks,
        pre_generation=result.pre_generation,
        post_generation=result.post_generation,
        needs_replan=not result.ok,
    )
