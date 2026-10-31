"""
ClarityOS — Provider error classification
========================================
One place that turns a raw provider exception into an actionable category.

Everything above this module (router, planner, agent, UI) branches on
`ProviderErrorKind` instead of pattern-matching exception strings, so a quota
failure is never reported to the user as "try rephrasing".

Quota note (Gemini free tier)
-----------------------------
`quotaId` tells daily exhaustion apart from a short rate-limit burst:

    GenerateRequestsPerDayPerProjectPerModel-FreeTier   -> 20/day, resets ~midnight PT
    GenerateRequestsPerMinutePerProjectPerModel-...     -> retry in seconds

The daily bucket is **per project**, not per key, so issuing a new API key in
the same Google AI Studio project does not restore quota.

`quotaDimensions.model` is the model Google *billed*, which can differ from the
model that was requested: public ids like `gemini-3.5-flash` are aliases that
resolve to a dated family member (e.g. `gemini-3.6-flash`) and share its bucket.
We key the exhaustion cache on that resolved name so every alias of the same
family is skipped together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

__all__ = ["ProviderErrorKind", "ProviderError", "classify_provider_error"]


class ProviderErrorKind(str, Enum):
    """Actionable failure categories. The UI shows a different message per kind."""

    QUOTA_DAILY = "quota_daily"            # daily allowance gone; retrying today cannot work
    QUOTA_RATE = "quota_rate"              # short burst limit; retrying shortly can work
    AUTH = "auth"                          # missing/invalid/!enabled API key
    MODEL_UNAVAILABLE = "model_unavailable"  # unknown, retired or unsupported model id
    OVERLOADED = "overloaded"              # provider-side capacity (503)
    NETWORK = "network"                    # DNS/TLS/connection/timeout
    UNKNOWN = "unknown"


# Kinds where issuing the same request again may succeed without changing config.
_RETRYABLE = {ProviderErrorKind.QUOTA_RATE, ProviderErrorKind.OVERLOADED, ProviderErrorKind.NETWORK}

_RETRY_DELAY_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?([\d.]+)s")

# A daily bucket is long; anything beyond this is not worth retrying in-request.
LONG_RETRY_THRESHOLD_S = 300.0


@dataclass
class ProviderError:
    """A provider failure, classified and safe to log."""

    kind: ProviderErrorKind
    detail: str                                  # raw provider message, for local logs
    requested_model: str = ""                    # what we asked for
    billed_model: str = ""                       # what the quota was charged against
    retry_after_s: Optional[float] = None
    quota_id: str = ""
    quota_limit: str = ""
    http_code: Optional[int] = None
    raw: Any = field(default=None, repr=False)

    @property
    def retryable(self) -> bool:
        """Whether re-issuing the identical request could plausibly succeed."""
        if self.kind not in _RETRYABLE:
            return False
        # A "rate" limit that wants us back in hours is a daily bucket in disguise.
        if self.retry_after_s is not None and self.retry_after_s > LONG_RETRY_THRESHOLD_S:
            return False
        return True

    @property
    def exhausted_model(self) -> str:
        """Model whose quota is spent — the billed name, so aliases collapse together."""
        if self.kind is not ProviderErrorKind.QUOTA_DAILY:
            return ""
        return self.billed_model or self.requested_model

    def retry_hint(self) -> str:
        """Human phrasing for when to come back. Empty when we genuinely don't know."""
        s = self.retry_after_s
        if s is None:
            return ""
        if s < 90:
            return f"about {int(s)} seconds"
        if s < 5400:
            return f"about {round(s / 60)} minutes"
        return f"about {round(s / 3600)} hours"

    def user_message(self) -> str:
        """Short, actionable, no secrets — safe to show in the UI."""
        model = self.billed_model or self.requested_model or "the model"
        hint = self.retry_hint()
        if self.kind is ProviderErrorKind.QUOTA_DAILY:
            limit = f" ({self.quota_limit}/day)" if self.quota_limit else ""
            when = f" Quota resets in {hint}." if hint else ""
            return (
                f"The daily free-tier quota for {model}{limit} is used up, so I can't plan this "
                f"task right now.{when} This limit is per Google Cloud project — a new API key in "
                f"the same project won't reset it. Enable billing or configure a different model to continue."
            )
        if self.kind is ProviderErrorKind.QUOTA_RATE:
            when = f" Try again in {hint}." if hint else " Try again shortly."
            return f"{model} is rate-limited right now.{when}"
        if self.kind is ProviderErrorKind.AUTH:
            return ("The Gemini API key was rejected. A key saved in Settings → API keys "
                    "overrides backend/.env, so clear or re-enter it there first; otherwise "
                    "check GEMINI_API_KEY in backend/.env.")
        if self.kind is ProviderErrorKind.MODEL_UNAVAILABLE:
            return (f"The configured model '{self.requested_model or model}' is not available to this "
                    f"API key. Update GEMINI_FAST_MODEL / GEMINI_POWERFUL_MODEL in backend/.env.")
        if self.kind is ProviderErrorKind.OVERLOADED:
            when = f" Try again in {hint}." if hint else " Try again in a moment."
            return f"{model} is temporarily overloaded on Google's side.{when}"
        if self.kind is ProviderErrorKind.NETWORK:
            return "I couldn't reach the Gemini API. Check your internet connection and try again."
        # An empty detail rendered as "The AI model failed to respond ()." —
        # parentheses around nothing, which tells the user less than saying
        # nothing would have. Several exceptions that land here carry no
        # message at all, so only show the brackets when there is something
        # in them.
        detail = (self.detail or "").strip()
        if not detail:
            return ("The AI model failed to respond, and gave no reason. "
                    "This is usually a dropped connection mid-answer; try again.")
        return f"The AI model failed to respond ({detail[:120]})."

    def log_line(self) -> str:
        """One-line local log record. Never contains credentials."""
        bits = [f"kind={self.kind.value}"]
        if self.http_code:
            bits.append(f"http={self.http_code}")
        if self.requested_model:
            bits.append(f"requested={self.requested_model}")
        if self.billed_model and self.billed_model != self.requested_model:
            bits.append(f"billed={self.billed_model}")
        if self.quota_id:
            bits.append(f"quota={self.quota_id}")
        if self.retry_after_s is not None:
            bits.append(f"retry_after={self.retry_after_s:.0f}s")
        return " ".join(bits)


def _parse_retry_delay(value: str) -> Optional[float]:
    """'37666s' / '10h27m46.5s' -> seconds."""
    if not value:
        return None
    text = str(value).strip()
    m = _RETRY_DELAY_RE.fullmatch(text)
    if not m:
        return None
    hours, minutes, seconds = m.group(1), m.group(2), m.group(3)
    try:
        total = float(seconds or 0)
    except ValueError:
        return None
    total += int(hours or 0) * 3600 + int(minutes or 0) * 60
    return total


def _error_body(details: Any) -> dict:
    """google-genai stores the whole response JSON; the useful part is details['error']."""
    if isinstance(details, dict):
        inner = details.get("error")
        if isinstance(inner, dict):
            return inner
        return details
    return {}


def _is_network_error(exc: BaseException) -> bool:
    names = {type(e).__name__ for e in (exc, exc.__cause__, exc.__context__) if e is not None}
    if names & {"ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException",
                "ClientConnectorError", "ServerDisconnectedError", "TimeoutError",
                "ConnectionResetError", "ConnectionError", "gaierror", "SSLError"}:
        return True
    text = str(exc).lower()
    return any(s in text for s in ("failed to establish a new connection", "name or service not known",
                                   "temporary failure in name resolution", "connection aborted"))


def _kind_from_code(code: Optional[int], status: str, message: str) -> ProviderErrorKind:
    if code == 429:
        return ProviderErrorKind.QUOTA_RATE          # refined to DAILY by quotaId below
    if code in (401, 403):
        return ProviderErrorKind.AUTH
    if code == 404:
        return ProviderErrorKind.MODEL_UNAVAILABLE
    if code == 503:
        return ProviderErrorKind.OVERLOADED
    if code == 400:
        low = message.lower()
        if "api key" in low or "api_key" in low:
            return ProviderErrorKind.AUTH
        if "model" in low and ("not found" in low or "not supported" in low):
            return ProviderErrorKind.MODEL_UNAVAILABLE
    if status == "RESOURCE_EXHAUSTED":
        return ProviderErrorKind.QUOTA_RATE
    if status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return ProviderErrorKind.AUTH
    if status == "NOT_FOUND":
        return ProviderErrorKind.MODEL_UNAVAILABLE
    if status == "UNAVAILABLE":
        return ProviderErrorKind.OVERLOADED
    return ProviderErrorKind.UNKNOWN


def classify_provider_error(exc: BaseException, requested_model: str = "") -> ProviderError:
    """
    Turn any provider exception into a `ProviderError`.

    Never raises: an unparseable failure becomes ProviderErrorKind.UNKNOWN so
    callers always get something they can branch on and log.
    """
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "")
    message = str(getattr(exc, "message", "") or str(exc))
    details = getattr(exc, "details", None)

    if code is None and _is_network_error(exc):
        return ProviderError(
            kind=ProviderErrorKind.NETWORK,
            detail=message[:500],
            requested_model=requested_model,
            raw=exc,
        )

    body = _error_body(details)
    if not status:
        status = str(body.get("status", "") or "")
    if not message:
        message = str(body.get("message", "") or "")
    if code is None:
        code = body.get("code")

    kind = _kind_from_code(code, status, message)

    billed_model = ""
    quota_id = ""
    quota_limit = ""
    retry_after: Optional[float] = None

    for entry in body.get("details", []) or []:
        if not isinstance(entry, dict):
            continue
        etype = str(entry.get("@type", "")).rsplit(".", 1)[-1]
        if etype == "QuotaFailure":
            for violation in entry.get("violations", []) or []:
                if not isinstance(violation, dict):
                    continue
                quota_id = quota_id or str(violation.get("quotaId", ""))
                quota_limit = quota_limit or str(violation.get("quotaValue", ""))
                dims = violation.get("quotaDimensions") or {}
                if isinstance(dims, dict) and dims.get("model"):
                    billed_model = billed_model or str(dims["model"])
        elif etype == "RetryInfo":
            retry_after = retry_after if retry_after is not None else _parse_retry_delay(entry.get("retryDelay", ""))

    # Daily vs burst: quotaId is authoritative, the retry delay is the fallback signal.
    if kind is ProviderErrorKind.QUOTA_RATE:
        if "PerDay" in quota_id or "per day" in message.lower():
            kind = ProviderErrorKind.QUOTA_DAILY
        elif retry_after is not None and retry_after > LONG_RETRY_THRESHOLD_S:
            kind = ProviderErrorKind.QUOTA_DAILY

    return ProviderError(
        kind=kind,
        detail=message[:500],
        requested_model=requested_model,
        billed_model=billed_model,
        retry_after_s=retry_after,
        quota_id=quota_id,
        quota_limit=quota_limit,
        http_code=code if isinstance(code, int) else None,
        raw=exc,
    )
