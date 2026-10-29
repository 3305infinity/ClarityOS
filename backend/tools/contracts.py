"""
Moonwalk — Typed tool and bridge envelopes.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional


def _base_meta(
    *,
    request_id: str = "",
    session_id: str = "",
    duration_ms: int = 0,
    provenance: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    meta = {
        "request_id": request_id or "",
        "session_id": session_id or "",
        "duration_ms": max(0, int(duration_ms or 0)),
        "provenance": provenance or "",
        "timestamp_ms": int(time.time() * 1000),
    }
    if extra:
        meta.update({k: v for k, v in extra.items() if v is not None})
    return meta


def success_envelope(
    data: Optional[dict[str, Any]] = None,
    *,
    request_id: str = "",
    session_id: str = "",
    duration_ms: int = 0,
    provenance: str = "",
    meta_extra: Optional[dict[str, Any]] = None,
    flatten: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "data": data or {},
        "error": None,
        "meta": _base_meta(
            request_id=request_id,
            session_id=session_id,
            duration_ms=duration_ms,
            provenance=provenance,
            extra=meta_extra,
        ),
    }
    if flatten and data:
        payload.update(data)
    return payload


def error_envelope(
    code: str,
    message: str,
    *,
    request_id: str = "",
    session_id: str = "",
    duration_ms: int = 0,
    provenance: str = "",
    retryable: bool = False,
    degraded: bool = False,
    source: str = "",
    details: Optional[dict[str, Any]] = None,
    meta_extra: Optional[dict[str, Any]] = None,
    flatten_details: bool = False,
) -> dict[str, Any]:
    error = {
        "code": code or "tool.unknown",
        "message": message or "",
        "retryable": bool(retryable),
        "degraded": bool(degraded),
        "source": source or "",
        "details": details or {},
    }
    payload: dict[str, Any] = {
        "ok": False,
        "data": None,
        "error": error,
        "meta": _base_meta(
            request_id=request_id,
            session_id=session_id,
            duration_ms=duration_ms,
            provenance=provenance,
            extra=meta_extra,
        ),
        # Legacy compatibility while callers migrate to payload["error"]["code"].
        "message": message or "",
        "error_code": code or "tool.unknown",
    }
    if flatten_details and details:
        payload.update(details)
    return payload


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
