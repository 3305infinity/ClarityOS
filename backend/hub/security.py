"""Host / Origin / token checks shared by the website API, WebSockets and the extension bridge."""
from __future__ import annotations

import hmac
import os
from typing import Iterable, Optional


def constant_eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _extra(env: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(env, "").split(",") if x.strip()}


def allowed_hosts(port: int) -> set[str]:
    base = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    return base | _extra("MOONWALK_ALLOWED_HOSTS")


def host_allowed(host_header: Optional[str], port: int) -> bool:
    """DNS-rebinding defence: only our own loopback names are accepted in `Host`."""
    return (host_header or "").strip().lower() in allowed_hosts(port)


def allowed_origins(port: int) -> set[str]:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"} | _extra("MOONWALK_ALLOWED_ORIGINS")


def origin_allowed(origin: Optional[str], port: int, *, allow_extension: bool = False,
                   allow_missing: bool = False) -> bool:
    """
    A browser always sends Origin on cross-site WebSocket / fetch requests, so a malicious
    web page can never look like our own site or our extension.
    `allow_missing` is for non-browser clients (the helper process), which send no Origin.
    """
    if not origin:
        return allow_missing
    origin = origin.strip().lower()
    if allow_extension and origin.startswith("chrome-extension://"):
        return True
    return origin in allowed_origins(port)


def bearer_from_header(value: Optional[str]) -> str:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""
