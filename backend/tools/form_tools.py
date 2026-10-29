"""
Moonwalk — Form Fill Tools
============================
Compound tool that uses vault memory data to intelligently fill web forms.

• fill_form – Reads form fields from the current page, matches them against
              vault data (contacts, documents, preferences), and fills them
              using browser_type_ref.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from tools.registry import registry


# ═══════════════════════════════════════════════════════════════
#  Field-label fuzzy matching
# ═══════════════════════════════════════════════════════════════

# Common form-field aliases → canonical keys that vault entries might store
_FIELD_ALIASES: dict[str, list[str]] = {
    "name":       ["name", "full name", "your name", "full_name", "fullname"],
    "first_name": ["first name", "first_name", "firstname", "given name", "forename"],
    "last_name":  ["last name", "last_name", "lastname", "surname", "family name"],
    "email":      ["email", "e-mail", "email address", "e-mail address", "your email"],
    "phone":      ["phone", "phone number", "telephone", "mobile", "cell", "contact number", "tel"],
    "address":    ["address", "street address", "street", "address line 1", "address_line_1"],
    "city":       ["city", "town", "town/city"],
    "postcode":   ["postcode", "post code", "zip", "zip code", "zipcode", "postal code"],
    "country":    ["country", "nationality"],
    "company":    ["company", "organisation", "organization", "employer", "company name"],
    "job_title":  ["job title", "role", "position", "title", "occupation"],
    "dob":        ["date of birth", "dob", "birthday", "birth date"],
    "website":    ["website", "url", "web address", "homepage"],
    "linkedin":   ["linkedin", "linkedin url", "linkedin profile"],
    "message":    ["message", "comments", "additional information", "notes", "cover letter"],
}

# Build reverse lookup: lowered alias → canonical key
_ALIAS_TO_KEY: dict[str, str] = {}
for _key, _aliases in _FIELD_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_KEY[_alias.lower()] = _key


def _match_field_label(label: str) -> Optional[str]:
    """Map a form-field label to a canonical key, or None if no match."""
    normed = " ".join((label or "").strip().lower().split())
    if normed in _ALIAS_TO_KEY:
        return _ALIAS_TO_KEY[normed]
    # Partial match: check if any alias is a substring
    for alias, key in _ALIAS_TO_KEY.items():
        if len(alias) >= 3 and alias in normed:
            return key
    return None


def _extract_fields_from_vault_entry(entry: dict) -> dict[str, str]:
    """Extract key-value fields from a vault entry's content and structured_data."""
    fields: dict[str, str] = {}

    # Structured data takes priority
    sd = entry.get("structured_data") or {}
    if isinstance(sd, dict):
        for k, v in sd.items():
            if isinstance(v, str) and v.strip():
                fields[k.lower().replace(" ", "_")] = v.strip()

    # Parse content for key: value patterns
    content = entry.get("content", "")
    for line in content.split("\n"):
        line = line.strip()
        m = re.match(r"^([A-Za-z\s_]+)\s*[:=]\s*(.+)$", line)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            val = m.group(2).strip()
            if key and val and key not in fields:
                fields[key] = val

    return fields


# ═══════════════════════════════════════════════════════════════
#  fill_form tool
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="fill_form",
    description=(
        "Intelligently fill a web form using data from the user's memory vault. "
        "Reads the current page's form fields (input/textarea elements), matches "
        "them against stored vault data (contacts, documents, preferences), and "
        "fills matching fields. Use recall_memory first if you need specific data, "
        "or this tool will automatically search the vault for relevant entries."
    ),
    parameters={
        "type": "object",
        "properties": {
            "vault_query": {
                "type": "string",
                "description": (
                    "Search query to find relevant vault data for filling "
                    "(e.g. 'my contact details', 'CV information', 'address')."
                ),
            },
            "vault_category": {
                "type": "string",
                "description": "Optional vault category to search within (e.g. 'contacts', 'documents').",
            },
            "field_overrides": {
                "type": "string",
                "description": (
                    "Optional JSON object of field_label→value overrides that take "
                    "priority over vault data (e.g. '{\"company\": \"Acme Corp\"}')"
                ),
            },
            "session_id": {
                "type": "string",
                "description": "Optional browser session id.",
            },
        },
        "required": ["vault_query"],
    },
)
async def fill_form(
    vault_query: str,
    vault_category: str = "",
    field_overrides: str = "",
    session_id: str = "",
) -> str:
    """Fill form fields by matching vault data to page elements."""

    # ── 1. Get vault data ──
    from tools.vault_tools import get_vault
    vault = get_vault()
    entries = vault.recall(
        query=vault_query,
        category=vault_category,
        max_results=5,
    )
    if not entries:
        return json.dumps({
            "status": "no_vault_data",
            "message": (
                f"No relevant data found in vault for '{vault_query}'. "
                "Store information with remember_this first."
            ),
        })

    # Merge all vault fields (earlier entries = higher priority)
    vault_fields: dict[str, str] = {}
    for entry in reversed(entries):
        vault_fields.update(_extract_fields_from_vault_entry(entry))

    # Apply overrides
    if field_overrides:
        try:
            overrides = json.loads(field_overrides)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    vault_fields[k.lower().replace(" ", "_")] = str(v)
        except (json.JSONDecodeError, TypeError):
            pass

    if not vault_fields:
        return json.dumps({
            "status": "no_fields",
            "message": "Vault entries found but no structured fields could be extracted.",
            "hint": "Store data with key: value format or use structured_data.",
        })

    # ── 2. Get current page form fields ──
    from browser.store import browser_store
    snapshot = browser_store.latest_snapshot(session_id=session_id or None)
    if not snapshot or not getattr(snapshot, "elements", None):
        return json.dumps({
            "status": "no_snapshot",
            "message": "No browser snapshot available. Navigate to the form page first.",
        })

    # Find fillable form elements
    fillable: list[dict[str, Any]] = []
    for el in snapshot.elements:
        tag = (getattr(el, "tag", "") or "").lower()
        role = (getattr(el, "role", "") or "").lower()
        if tag not in ("input", "textarea", "select") and role not in ("textbox", "combobox", "spinbutton"):
            continue
        if not getattr(el, "visible", True):
            continue
        el_type = (getattr(el, "input_type", "") or getattr(el, "type", "") or "").lower()
        if el_type in ("hidden", "submit", "button", "image", "reset", "file", "checkbox", "radio"):
            continue
        ref_id = str(getattr(el, "ref_id", "") or "").strip()
        if not ref_id:
            continue
        label = (
            getattr(el, "aria_label", "")
            or getattr(el, "name", "")
            or getattr(el, "placeholder", "")
            or getattr(el, "text", "")
            or getattr(el, "label", "")
            or ""
        ).strip()
        fillable.append({
            "ref_id": ref_id,
            "label": label,
            "tag": tag,
            "type": el_type,
            "current_value": (getattr(el, "value", "") or "").strip(),
        })

    if not fillable:
        return json.dumps({
            "status": "no_form_fields",
            "message": "No fillable form fields found on the current page.",
        })

    # ── 3. Match vault fields → form fields ──
    fill_plan: list[dict[str, str]] = []
    for form_field in fillable:
        if form_field["current_value"]:
            continue  # Skip already-filled fields
        canonical = _match_field_label(form_field["label"])
        if canonical and canonical in vault_fields:
            fill_plan.append({
                "ref_id": form_field["ref_id"],
                "label": form_field["label"],
                "matched_key": canonical,
                "value": vault_fields[canonical],
            })

    if not fill_plan:
        return json.dumps({
            "status": "no_matches",
            "message": "Could not match any form fields to vault data.",
            "form_fields": [f["label"] for f in fillable[:15]],
            "vault_keys": list(vault_fields.keys())[:15],
            "hint": "You can manually use browser_type_ref to fill specific fields.",
        })

    # ── 4. Fill the fields via browser_type_ref ──
    filled: list[dict] = []
    failed: list[dict] = []

    for plan_item in fill_plan:
        try:
            from tools.browser_aci import _require_snapshot
            # Use registry.execute to call browser_type_ref
            result = await registry.execute("browser_type_ref", {
                "ref_id": plan_item["ref_id"],
                "text": plan_item["value"],
                "reasoning": f"Filling form field '{plan_item['label']}' with vault data",
            })
            filled.append({
                "label": plan_item["label"],
                "key": plan_item["matched_key"],
                "ref_id": plan_item["ref_id"],
                "value": plan_item["value"][:50],
            })
        except Exception as e:
            failed.append({
                "label": plan_item["label"],
                "error": str(e)[:100],
            })

    return json.dumps({
        "status": "success" if filled else "partial",
        "filled_count": len(filled),
        "failed_count": len(failed),
        "filled": filled,
        "failed": failed if failed else None,
        "message": f"Filled {len(filled)} form field(s) from vault data.",
    }, ensure_ascii=False)
