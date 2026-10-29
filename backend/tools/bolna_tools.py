"""
ClarityOS — Bolna voice-agent tools
===================================
Bolna (https://www.bolna.ai) runs AI *phone-call* agents. It is not a speech-to-text / text-to-speech
service: you build an agent in the Bolna dashboard (prompt, voice, language) and then ask Bolna to
call a phone number. These tools let ClarityOS do that on the user's behalf.

    BOLNA_API_KEY     required (Settings → API keys)       sent as  Authorization: Bearer
    BOLNA_AGENT_ID    default agent to use (Settings → Voice agents)
    BOLNA_API_BASE    default https://api.bolna.ai
    BOLNA_ALLOWED_PREFIXES   optional, e.g. "+91,+1" — refuse numbers outside these country codes

Endpoints used (Bolna API):
    POST /call                  { agent_id, recipient_phone_number (E.164), from_phone_number?, user_data? }
    GET  /executions/{id}       status, transcript, recording, extracted data

A real phone call costs money and reaches a real person, so the hub's policy engine requires the
user's approval for `bolna_call` every time (see hub/policy.py).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from tools.registry import registry

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _base() -> str:
    return os.environ.get("BOLNA_API_BASE", "https://api.bolna.ai").rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('BOLNA_API_KEY', '')}", "Content-Type": "application/json"}


def normalize_phone(number: str) -> Optional[str]:
    """Return an E.164 number or None. Accepts spaces/dashes/parentheses; requires a leading +."""
    n = re.sub(r"[\s\-().]", "", number or "")
    return n if _E164.match(n) else None


def _find(obj: Any, *keys: str) -> Any:
    """First value for any of `keys`, searched recursively (Bolna nests telephony data)."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, "", [], {}):
                return obj[k]
        for v in obj.values():
            r = _find(v, *keys)
            if r not in (None, "", [], {}):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, *keys)
            if r not in (None, "", [], {}):
                return r
    return None


@registry.register(
    name="bolna_call",
    description=(
        "Place a real outbound PHONE CALL through a Bolna AI voice agent. Use ONLY when the user explicitly "
        "asks to phone/call a person or business (e.g. 'Rahul ko call karo', 'call the restaurant and book a table'). "
        "The number must be in international format like +919876543210. The call costs money and reaches a real "
        "person, so confirm the number with the user first. Returns an execution_id; poll bolna_call_status for the "
        "outcome/transcript. Optional user_data is a JSON object of variables for the agent prompt "
        "(e.g. {\"customer_name\": \"Asha\"})."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient_phone_number": {"type": "string", "description": "Number to call, E.164 (+countrycode…)"},
            "agent_id": {"type": "string", "description": "Bolna agent id. Optional — defaults to the one in Settings."},
            "from_phone_number": {"type": "string", "description": "Optional caller-ID number owned in the Bolna account"},
            "user_data": {"type": "string", "description": "Optional JSON object string of variables for the agent"},
        },
        "required": ["recipient_phone_number"],
    },
    timeout=30,
)
async def bolna_call(recipient_phone_number: str, agent_id: str = "", from_phone_number: str = "", user_data: str = "") -> str:
    import httpx

    if not os.environ.get("BOLNA_API_KEY"):
        return "ERROR: Bolna is not configured. Add the Bolna API key in Settings → API keys."
    number = normalize_phone(recipient_phone_number)
    if not number:
        return "ERROR: recipient_phone_number must be in international format, e.g. +919876543210"
    prefixes = [p.strip() for p in os.environ.get("BOLNA_ALLOWED_PREFIXES", "").split(",") if p.strip()]
    if prefixes and not any(number.startswith(p) for p in prefixes):
        return f"BLOCKED: calls are restricted to these country codes: {', '.join(prefixes)}"
    agent = (agent_id or os.environ.get("BOLNA_AGENT_ID", "")).strip()
    if not agent or not _UUID.match(agent):
        return "ERROR: no Bolna agent selected. Set the Agent ID in Settings → Voice agents."

    body: dict = {"agent_id": agent, "recipient_phone_number": number}
    if from_phone_number:
        frm = normalize_phone(from_phone_number)
        if not frm:
            return "ERROR: from_phone_number must be in international format"
        body["from_phone_number"] = frm
    if user_data:
        try:
            data = json.loads(user_data)
            if not isinstance(data, dict):
                raise ValueError
            body["user_data"] = data
        except ValueError:
            return "ERROR: user_data must be a JSON object string"

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(f"{_base()}/call", json=body, headers=_headers())
    except httpx.HTTPError as e:
        return f"ERROR: could not reach Bolna ({e.__class__.__name__})"
    if resp.status_code == 401:
        return "ERROR: Bolna rejected the API key (401). Check Settings → API keys."
    if resp.status_code >= 400:
        return f"ERROR: Bolna returned {resp.status_code}: {(resp.text or '')[:250]}"
    data = resp.json() if resp.content else {}
    eid = _find(data, "execution_id", "run_id", "id") or "unknown"
    return f"Call queued with Bolna (execution_id={eid}, status={data.get('status', 'queued')}). Use bolna_call_status to check the result."


@registry.register(
    name="bolna_call_status",
    description="Check the outcome of a Bolna phone call: status, duration, transcript, recording link and extracted data.",
    parameters={
        "type": "object",
        "properties": {"execution_id": {"type": "string", "description": "execution_id returned by bolna_call"}},
        "required": ["execution_id"],
    },
    timeout=30,
)
async def bolna_call_status(execution_id: str) -> str:
    import httpx

    if not os.environ.get("BOLNA_API_KEY"):
        return "ERROR: Bolna is not configured. Add the Bolna API key in Settings → API keys."
    if not _UUID.match(execution_id or ""):
        return "ERROR: invalid execution_id"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.get(f"{_base()}/executions/{execution_id}", headers=_headers())
    except httpx.HTTPError as e:
        return f"ERROR: could not reach Bolna ({e.__class__.__name__})"
    if resp.status_code >= 400:
        return f"ERROR: Bolna returned {resp.status_code}: {(resp.text or '')[:250]}"
    d = resp.json()
    out = {
        "status": _find(d, "status"),
        "duration_seconds": _find(d, "conversation_time", "conversation_duration", "duration"),
        "hangup_reason": _find(d, "hangup_reason"),
        "transcript": (str(_find(d, "transcript") or ""))[:2500],
        "recording_url": _find(d, "recording_url"),
        "extracted_data": _find(d, "extracted_data"),
    }
    return json.dumps({k: v for k, v in out.items() if v not in (None, "")}, ensure_ascii=False)
