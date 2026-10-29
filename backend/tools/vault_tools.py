"""
Moonwalk — Vault Memory Tools
===============================
Registered tools that let the LLM store, recall, and manage permanent
cross-session memory entries via the VaultMemory system.

Three tools:
  • remember_this   – Store information in the permanent vault
  • recall_memory   – Search/retrieve stored memories by query, category, or tags
  • forget_this     – Delete a vault entry by ID
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry
from agent.memory import VaultMemory, VAULT_CATEGORIES

# ── Singleton vault instance (shared across tools) ──
_vault = VaultMemory()


def get_vault() -> VaultMemory:
    """Return the global VaultMemory instance for use by other modules."""
    return _vault


# ═══════════════════════════════════════════════════════════════
#  remember_this — store to vault
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="remember_this",
    description=(
        "Store information permanently in the user's memory vault so it persists "
        "across sessions. Use this for contacts, preferences, research findings, "
        "shopping lists, important notes, documents, or anything the user wants "
        "remembered long-term. Categories: "
        + ", ".join(sorted(VAULT_CATEGORIES))
        + "."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": (
                    "Category for the entry. One of: "
                    + ", ".join(sorted(VAULT_CATEGORIES))
                ),
            },
            "title": {
                "type": "string",
                "description": "Short descriptive title for the entry (e.g. 'Mum phone number', 'Budget for laptop').",
            },
            "content": {
                "type": "string",
                "description": "The text content to store. Can be free-form notes, structured text, or a summary.",
            },
            "tags": {
                "type": "string",
                "description": "Optional comma-separated tags for easier recall (e.g. 'family,phone' or 'budget,tech').",
            },
            "source": {
                "type": "string",
                "description": "Optional source URL or context where this information came from.",
            },
        },
        "required": ["category", "title", "content"],
    },
)
async def remember_this(
    category: str,
    title: str,
    content: str,
    tags: str = "",
    source: str = "",
) -> str:
    """Store an entry in the permanent memory vault."""
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    result = _vault.store(
        category=category,
        title=title,
        content=content,
        tags=tag_list if tag_list else None,
        source=source,
    )
    if result.get("ok"):
        action = result.get("action", "created")
        return json.dumps({
            "status": "success",
            "action": action,
            "id": result.get("id", ""),
            "message": f"{'Updated' if action == 'updated' else 'Stored'} in vault [{category}]: {title}",
        })
    return json.dumps({
        "status": "error",
        "message": result.get("error", "Failed to store entry."),
    })


# ═══════════════════════════════════════════════════════════════
#  recall_memory — search the vault
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="recall_memory",
    description=(
        "Search the user's permanent memory vault for stored information. "
        "Returns matching entries ranked by relevance. Use this to find contacts, "
        "preferences, research notes, shopping lists, or any previously saved data. "
        "You can search by free-text query, category, tags, or any combination."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search query (e.g. 'mum phone number', 'laptop budget').",
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category filter. One of: "
                    + ", ".join(sorted(VAULT_CATEGORIES))
                ),
            },
            "tags": {
                "type": "string",
                "description": "Optional comma-separated tags to filter by (e.g. 'family,phone').",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default 10).",
            },
        },
        "required": [],
    },
)
async def recall_memory(
    query: str = "",
    category: str = "",
    tags: str = "",
    max_results: int = 10,
) -> str:
    """Search the permanent memory vault."""
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    results = _vault.recall(
        query=query,
        category=category,
        tags=tag_list,
        max_results=max_results,
    )
    if not results:
        return json.dumps({
            "status": "no_results",
            "message": "No matching entries found in the vault.",
            "query": query,
            "category": category,
        })
    # Format results for the LLM
    formatted = []
    for entry in results:
        formatted.append({
            "id": entry.get("id", ""),
            "category": entry.get("category", ""),
            "title": entry.get("title", ""),
            "content": entry.get("content", entry.get("content_preview", "")),
            "tags": entry.get("tags", []),
            "source": entry.get("source", ""),
            "updated_at": entry.get("updated_at", 0),
        })
    return json.dumps({
        "status": "success",
        "count": len(formatted),
        "results": formatted,
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  forget_this — delete from vault
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="forget_this",
    description=(
        "Delete an entry from the user's permanent memory vault by its ID. "
        "Use recall_memory first to find the entry ID, then call this to remove it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "The ID of the vault entry to delete (from recall_memory results).",
            },
        },
        "required": ["entry_id"],
    },
)
async def forget_this(entry_id: str) -> str:
    """Delete a vault entry by ID."""
    deleted = _vault.delete(entry_id)
    if deleted:
        return json.dumps({
            "status": "success",
            "message": f"Deleted vault entry: {entry_id}",
        })
    return json.dumps({
        "status": "not_found",
        "message": f"No vault entry found with ID: {entry_id}",
    })
