"""
Log / event redaction.

Everything that is persisted (task events, exported logs) or sent to the UI as a tool
argument passes through `redact()`. Two layers:
  1. exact-match: every currently configured API key value is replaced
  2. pattern-match: well-known key shapes and  key=value / "key": "value"  pairs
Large blobs (base64 screenshots/audio) are replaced by a size marker.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

MASK = "[REDACTED]"
MAX_STR = 4000

_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                   # Google API keys
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),                  # OpenAI-style
    re.compile(r"\bbn-[A-Za-z0-9]{16,}"),                     # Bolna
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}"),   # bearer tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
]
_KV = re.compile(
    r"""(?ix)
    (?P<k>["']?(?:api[_-]?key|x-api-key-id|api-subscription-key|authorization|token|secret|password|passwd|pwd|otp|cvv|card[_-]?number)["']?\s*[:=]\s*)
    (?P<q>["']?)(?P<v>[^\s"',;}]{3,})(?P=q)
    """
)
_BASE64_BLOB = re.compile(r"^[A-Za-z0-9+/=\s]{2000,}$")
_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|audio|screenshot|image_b64|jpeg_b64)")


def make_redactor(secret_values: Callable[[], Iterable[str]] = lambda: ()):
    def redact_str(s: str) -> str:
        for v in secret_values():
            if v and v in s:
                s = s.replace(v, MASK)
        for p in _PATTERNS:
            s = p.sub(MASK, s)
        s = _KV.sub(lambda m: f"{m.group('k')}{m.group('q')}{MASK}{m.group('q')}", s)
        if _BASE64_BLOB.match(s or ""):
            return f"<{len(s)} chars of binary data>"
        if len(s) > MAX_STR:
            s = s[:MAX_STR] + f"…[+{len(s) - MAX_STR} chars]"
        return s

    def redact(obj: Any, _key: str = "") -> Any:
        if isinstance(obj, str):
            if _key and _SENSITIVE_KEYS.search(_key) and len(obj) > 0:
                if re.search(r"(?i)audio|screenshot|image|jpeg", _key):
                    return f"<{len(obj)} chars>"
                return MASK
            return redact_str(obj)
        if isinstance(obj, dict):
            return {str(k): redact(v, str(k)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [redact(v, _key) for v in obj]
        if isinstance(obj, bytes):
            return f"<{len(obj)} bytes>"
        return obj

    redact.string = redact_str  # type: ignore[attr-defined]
    return redact
