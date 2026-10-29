"""
Moonwalk — Authentication Module
==================================
Verifies incoming connections using either:
  1. Google ID tokens (production — verified via Google's certs)
  2. Shared secret tokens (simple pre-shared key)

Usage in cloud_server.py:
  from auth import verify_connection, AuthResult
  result = verify_connection(auth_message)
  if not result.ok:
      await ws.close(4001, result.error)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx


# ═══════════════════════════════════════════════════════════════
#  Auth Result
# ═══════════════════════════════════════════════════════════════

@dataclass
class AuthResult:
    ok: bool
    user_id: str = ""
    email: str = ""
    name: str = ""
    picture: str = ""
    method: str = ""       # "google" | "token" | "anonymous"
    error: str = ""


# ═══════════════════════════════════════════════════════════════
#  Google ID Token Verification
# ═══════════════════════════════════════════════════════════════

# Google's public key endpoints
_GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Allowed Google OAuth Client IDs (set via env)
_ALLOWED_CLIENT_IDS: set[str] = set()


def _load_allowed_client_ids() -> set[str]:
    """Load allowed Google OAuth client IDs from environment."""
    global _ALLOWED_CLIENT_IDS
    raw = os.environ.get("MOONWALK_GOOGLE_CLIENT_ID", "")
    if raw:
        _ALLOWED_CLIENT_IDS = {cid.strip() for cid in raw.split(",") if cid.strip()}
    return _ALLOWED_CLIENT_IDS


async def verify_google_id_token(id_token: str) -> AuthResult:
    """
    Verify a Google ID token by calling Google's tokeninfo endpoint.
    Returns an AuthResult with the user's Google profile info.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _GOOGLE_TOKENINFO_URL,
                params={"id_token": id_token},
            )

            if resp.status_code != 200:
                return AuthResult(ok=False, error=f"Google token verification failed: {resp.status_code}")

            payload = resp.json()

            # Validate the audience (client ID) if configured
            allowed = _load_allowed_client_ids()
            if allowed:
                aud = payload.get("aud", "")
                if aud not in allowed:
                    return AuthResult(
                        ok=False,
                        error=f"Token audience '{aud}' not in allowed client IDs",
                    )

            # Check expiration
            exp = int(payload.get("exp", 0))
            if exp and exp < time.time():
                return AuthResult(ok=False, error="Google ID token has expired")

            # Extract user info
            return AuthResult(
                ok=True,
                user_id=payload.get("sub", ""),
                email=payload.get("email", ""),
                name=payload.get("name", ""),
                picture=payload.get("picture", ""),
                method="google",
            )

    except httpx.TimeoutException:
        return AuthResult(ok=False, error="Google token verification timed out")
    except Exception as e:
        return AuthResult(ok=False, error=f"Google token verification error: {e}")


# ═══════════════════════════════════════════════════════════════
#  Shared Secret Token Verification
# ═══════════════════════════════════════════════════════════════

def verify_shared_token(token: str, user_id: str = "") -> AuthResult:
    """
    Verify a pre-shared secret token.
    The token must match MOONWALK_CLOUD_TOKEN env var.
    """
    expected = os.environ.get("MOONWALK_CLOUD_TOKEN", "")
    if not expected:
        return AuthResult(ok=False, error="Server has no MOONWALK_CLOUD_TOKEN configured")

    if not token or token != expected:
        return AuthResult(ok=False, error="Invalid authentication token")

    return AuthResult(
        ok=True,
        user_id=user_id or "token-user",
        method="token",
    )


# ═══════════════════════════════════════════════════════════════
#  Unified Connection Verifier
# ═══════════════════════════════════════════════════════════════

async def verify_connection(auth_data: dict) -> AuthResult:
    """
    Verify an incoming WebSocket connection.

    Expected auth_data format:
    {
        "type": "auth",
        "method": "google" | "token",
        "token": "<id_token_or_shared_secret>",
        "user_id": "<optional_user_id>"
    }

    Auth modes (checked in order):
    1. If method="google": verify Google ID token
    2. If method="token": verify shared secret
    3. If MOONWALK_CLOUD_TOKEN is empty: allow anonymous (dev mode)
    """
    if not isinstance(auth_data, dict):
        return AuthResult(ok=False, error="Invalid auth message format")

    if auth_data.get("type") != "auth":
        return AuthResult(ok=False, error="Expected auth message")

    method = auth_data.get("method", "token")
    token = auth_data.get("token", "")
    user_id = auth_data.get("user_id", "")

    # Google ID token verification
    if method == "google" and token:
        return await verify_google_id_token(token)

    # Shared secret token verification
    if method == "token" and token:
        return verify_shared_token(token, user_id)

    # If no MOONWALK_CLOUD_TOKEN is set, allow anonymous (dev mode)
    cloud_token = os.environ.get("MOONWALK_CLOUD_TOKEN", "")
    if not cloud_token:
        return AuthResult(
            ok=True,
            user_id=user_id or "anonymous",
            method="anonymous",
        )

    return AuthResult(ok=False, error="Authentication required")


# ═══════════════════════════════════════════════════════════════
#  Token Generation (for first-launch setup)
# ═══════════════════════════════════════════════════════════════

def generate_user_credentials() -> dict:
    """
    Generate a fresh user_id and auth_token for first-launch setup.
    These are stored in Electron's safeStorage and sent with each connection.
    """
    import secrets
    import uuid

    return {
        "user_id": str(uuid.uuid4()),
        "auth_token": secrets.token_urlsafe(48),
    }
