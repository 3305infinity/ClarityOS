"""
Moonwalk — Cloud-Persistent Memory (Firestore + GCS)
=====================================================
Drop-in replacements for the local ~/.moonwalk file-backed memory classes.
Each class keeps the same public interface so core_v2.py can swap in
cloud memory without touching the agent loop.

Storage mapping:
  ConversationMemory  → Firestore  users/{uid}/sessions/{sid}
  UserProfile         → Firestore  users/{uid}/profile
  UserPreferences     → Firestore  users/{uid}/preferences
  VaultMemory         → Firestore  users/{uid}/vault/{entry_id}
  TaskStore           → Firestore  users/{uid}/tasks/{task_id}
  WorkingMemory       → Redis / Memorystore (falls back to in-memory)

Large blobs (screenshots, documents >1 MB) are offloaded to a GCS bucket.
"""

from __future__ import annotations

import json
import os
import re
import time
import math
import uuid
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional, List

# Google Cloud SDKs (available in the cloud container)
from google.cloud import firestore  # type: ignore[import]
from google.cloud import storage as gcs  # type: ignore[import]

# ── Configuration ──

GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCS_BUCKET = os.environ.get("MOONWALK_GCS_BUCKET", f"{GCP_PROJECT}-moonwalk-memory")
DEFAULT_USER_ID = os.environ.get("MOONWALK_USER_ID", "default")

# Lazy-init singletons
_db: Optional[firestore.Client] = None
_gcs: Optional[gcs.Client] = None
_bucket = None


def _get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=GCP_PROJECT or None)
    return _db


def _get_bucket():
    global _gcs, _bucket
    if _bucket is None:
        _gcs = gcs.Client(project=GCP_PROJECT or None)
        _bucket = _gcs.bucket(GCS_BUCKET)
        # Create bucket if it doesn't exist
        if not _bucket.exists():
            _bucket = _gcs.create_bucket(GCS_BUCKET, location="us-central1")
            print(f"[CloudMemory] Created GCS bucket: {GCS_BUCKET}")
    return _bucket


def _user_ref(user_id: str = "") -> str:
    """Return the Firestore path prefix for a user."""
    return user_id or DEFAULT_USER_ID


# ═══════════════════════════════════════════════════════════════
#  Cloud Conversation Memory
# ═══════════════════════════════════════════════════════════════

class CloudConversationMemory:
    """Conversation history persisted in Firestore.

    Same interface as memory.ConversationMemory but backed by Firestore
    instead of ~/.moonwalk/sessions/*.json.
    """

    def __init__(
        self,
        max_turns: int = 20,
        idle_timeout: float = 300.0,
        user_id: str = "",
    ):
        self._max_turns = max_turns
        self._idle_timeout = idle_timeout
        self._user_id = _user_ref(user_id)
        self._session_id: str = uuid.uuid4().hex[:12]
        self._turns: list[dict] = []
        self._session_summary: str = ""
        self._last_activity: float = time.time()
        self._save_dirty = False
        self._save_timer: Optional[threading.Timer] = None
        self._io_lock = threading.Lock()

        # Try to resume a recent session from Firestore
        self._try_resume_session()

    def _col(self):
        return _get_db().collection("users").document(self._user_id).collection("sessions")

    def _try_resume_session(self, resume_window: float = 1800.0):
        try:
            docs = (
                self._col()
                .order_by("updated_at", direction=firestore.Query.DESCENDING)
                .limit(1)
                .stream()
            )
            for doc in docs:
                data = doc.to_dict()
                age = time.time() - data.get("updated_at", 0)
                if age <= resume_window:
                    self._turns = data.get("turns", [])
                    self._session_id = doc.id
                    self._session_summary = data.get("summary", "")
                    self._last_activity = data.get("updated_at", time.time())
                    print(f"[CloudMem] Resumed session {self._session_id} ({len(self._turns)} turns)")
        except Exception as e:
            print(f"[CloudMem] Session resume failed: {e}")

    def _save_session(self):
        self._save_dirty = True
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(2.0, self._flush_save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _flush_save(self):
        if not self._save_dirty:
            return
        self._save_dirty = False
        with self._io_lock:
            try:
                self._col().document(self._session_id).set({
                    "turns": self._turns[-self._max_turns:],
                    "summary": self._session_summary,
                    "updated_at": time.time(),
                })
            except Exception as e:
                print(f"[CloudMem] Save failed: {e}")

    def _check_timeout(self):
        if time.time() - self._last_activity > self._idle_timeout and self._turns:
            self._flush_save()
            self.start_new_session()

    # ── Public API (same as ConversationMemory) ──

    def add_user(self, text: str, context_summary: str = ""):
        self._check_timeout()
        self._last_activity = time.time()
        content = f"{text}\n\n{context_summary}" if context_summary else text
        self._turns.append({"role": "user", "parts": [{"text": content}]})
        self._trim()
        self._save_session()

    def add_model(self, text: str):
        self._last_activity = time.time()
        self._turns.append({"role": "model", "parts": [{"text": text}]})
        self._trim()
        self._save_session()

    def add_function_call(self, name: str, args: dict):
        self._last_activity = time.time()
        self._turns.append({
            "role": "model",
            "parts": [{"function_call": {"name": name, "args": args}}]
        })
        self._trim()

    def add_function_response(self, name: str, result: str):
        self._last_activity = time.time()
        self._turns.append({
            "role": "function",
            "parts": [{"function_response": {"name": name, "response": {"result": result}}}]
        })
        self._trim()

    def get_history(self) -> list[dict]:
        self._check_timeout()
        return list(self._turns)

    def get_session_summary(self) -> str:
        return self._session_summary

    def set_session_summary(self, summary: str):
        self._session_summary = summary
        self._save_session()

    def clear(self):
        self._flush_save()
        self._turns.clear()

    def start_new_session(self):
        self._flush_save()
        self._session_id = uuid.uuid4().hex[:12]
        self._turns.clear()
        self._session_summary = ""
        self._last_activity = time.time()

    def _trim(self):
        if len(self._turns) <= self._max_turns:
            return
        dropped = len(self._turns) - self._max_turns
        self._turns = self._turns[-self._max_turns:]
        if (
            self._turns
            and self._turns[0].get("role") == "model"
            and self._turns[0].get("parts", [{}])[0].get("text", "").startswith("[CONTEXT SUMMARY")
        ):
            self._turns[0]["parts"][0]["text"] = (
                f"[CONTEXT SUMMARY: {dropped} older turns were removed "
                f"from context to save memory.  Rely on long-term memory for older details.]"
            )


# ═══════════════════════════════════════════════════════════════
#  Cloud User Profile
# ═══════════════════════════════════════════════════════════════

class CloudUserProfile:
    """Drop-in for UserProfile — stores auto-extracted facts in Firestore."""

    FACT_PATTERNS = [
        (r'\bmy\s+([\w\s]+?)\s+(?:is|are|lives?\s+(?:at|in))\s+(.+?)(?:\.|$)',
         lambda m: (m.group(1).strip().lower(), m.group(2).strip())),
        (r'\bi\s+(?:use|prefer|like|work with)\s+(.+?)(?:\s+for\s+(.+?))?(?:\.|$)',
         lambda m: (f"preferred_{m.group(2).strip().lower()}" if m.group(2) else "preferred_tool",
                     m.group(1).strip())),
        (r'\bmy\s+preferred\s+([\w\s]+?)\s+is\s+(.+?)(?:\.|$)',
         lambda m: (f"preferred_{m.group(1).strip().lower()}", m.group(2).strip())),
        (r'\bremember\s+that\s+(.+?)(?:\.|$)',
         lambda m: ("remembered_fact", m.group(1).strip())),
        (r'\bprojects?\s+(?:live|are)\s+(?:in|at)\s+([~/]\S+)',
         lambda m: ("projects_directory", m.group(1).strip())),
    ]

    def __init__(self, user_id: str = ""):
        self._user_id = _user_ref(user_id)
        self._profile: dict = self._load()

    def _doc(self):
        return _get_db().collection("users").document(self._user_id).collection("meta").document("profile")

    def _load(self) -> dict:
        try:
            doc = self._doc().get()
            if doc.exists:
                return doc.to_dict()
        except Exception as e:
            print(f"[CloudProfile] Load failed: {e}")
        return {"facts": {}, "interaction_count": 0, "first_seen": time.time()}

    def _save(self):
        try:
            self._doc().set(self._profile)
        except Exception as e:
            print(f"[CloudProfile] Save failed: {e}")

    def extract_facts(self, user_text: str) -> List[tuple]:
        extracted = []
        text_lower = user_text.lower()
        for pattern, extractor in self.FACT_PATTERNS:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                try:
                    key, value = extractor(match)
                    key = re.sub(r'\s+', '_', key)
                    self._profile["facts"][key] = {
                        "value": value,
                        "source": user_text[:100],
                        "updated_at": time.time(),
                    }
                    extracted.append((key, value))
                except Exception:
                    continue
        if extracted:
            self._save()
        self._profile["interaction_count"] = self._profile.get("interaction_count", 0) + 1
        if self._profile["interaction_count"] % 10 == 0:
            self._save()
        return extracted

    def get_fact(self, key: str) -> Optional[str]:
        fact = self._profile.get("facts", {}).get(key)
        return fact["value"] if fact else None

    def get_all_facts(self) -> dict:
        return {k: v["value"] for k, v in self._profile.get("facts", {}).items()}

    def to_prompt_string(self) -> str:
        facts = self.get_all_facts()
        if not facts:
            return ""
        lines = ["[User Profile — remembered facts about this user]"]
        for key, value in facts.items():
            readable_key = key.replace("_", " ").title()
            lines.append(f"  - {readable_key}: {value}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Cloud User Preferences
# ═══════════════════════════════════════════════════════════════

class CloudUserPreferences:
    """Drop-in for UserPreferences — Firestore-backed."""

    def __init__(self, user_id: str = ""):
        self._user_id = _user_ref(user_id)
        self._prefs: dict = self._load()

    def _doc(self):
        return _get_db().collection("users").document(self._user_id).collection("meta").document("preferences")

    def _load(self) -> dict:
        try:
            doc = self._doc().get()
            if doc.exists:
                return doc.to_dict()
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            self._doc().set(self._prefs)
        except Exception as e:
            print(f"[CloudPrefs] Save failed: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._prefs.get(key, default)

    def set(self, key: str, value: Any):
        self._prefs[key] = value
        self._save()

    def get_all(self) -> dict:
        return dict(self._prefs)

    def to_prompt_string(self) -> str:
        if not self._prefs:
            return ""
        lines = ["[User Preferences]"]
        for k, v in self._prefs.items():
            lines.append(f"  - {k.replace('_', ' ').title()}: {v}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Cloud Task Store
# ═══════════════════════════════════════════════════════════════

@dataclass
class BackgroundTask:
    id: str
    description: str
    interval_seconds: float
    created_at: float
    last_run: float = 0.0
    active: bool = True


class CloudTaskStore:
    """Drop-in for TaskStore — Firestore-backed persistent tasks."""

    def __init__(self, user_id: str = ""):
        self._user_id = _user_ref(user_id)
        self._tasks: dict[str, BackgroundTask] = self._load()

    def _col(self):
        return _get_db().collection("users").document(self._user_id).collection("tasks")

    def _load(self) -> dict[str, BackgroundTask]:
        try:
            tasks = {}
            for doc in self._col().stream():
                d = doc.to_dict()
                tasks[doc.id] = BackgroundTask(**d)
            return tasks
        except Exception:
            return {}

    def _save_task(self, task: BackgroundTask):
        try:
            self._col().document(task.id).set({
                "id": task.id,
                "description": task.description,
                "interval_seconds": task.interval_seconds,
                "created_at": task.created_at,
                "last_run": task.last_run,
                "active": task.active,
            })
        except Exception as e:
            print(f"[CloudTasks] Save failed: {e}")

    def add(self, description: str, interval_seconds: float) -> BackgroundTask:
        tid = f"task_{int(time.time())}"
        task = BackgroundTask(
            id=tid,
            description=description,
            interval_seconds=interval_seconds,
            created_at=time.time(),
        )
        self._tasks[tid] = task
        self._save_task(task)
        return task

    def get_due(self) -> list[BackgroundTask]:
        now = time.time()
        return [t for t in self._tasks.values() if t.active and (now - t.last_run) >= t.interval_seconds]

    def mark_run(self, task_id: str):
        if task_id in self._tasks:
            self._tasks[task_id].last_run = time.time()
            self._save_task(self._tasks[task_id])

    def remove(self, task_id: str):
        if task_id in self._tasks:
            try:
                self._col().document(task_id).delete()
            except Exception:
                pass
            del self._tasks[task_id]

    def list_active(self) -> list[BackgroundTask]:
        return [t for t in self._tasks.values() if t.active]


# ═══════════════════════════════════════════════════════════════
#  Cloud Vault Memory (with RAG vector embeddings)
# ═══════════════════════════════════════════════════════════════

VAULT_CATEGORIES = frozenset({
    "notes", "contacts", "documents", "preferences",
    "research", "shopping", "conversations", "files",
})

_VAULT_MAX_ENTRIES = 2000       # higher cap in cloud
_VAULT_MAX_ENTRY_BYTES = 100000  # 100 KB per entry


class CloudVaultMemory:
    """Vault memory backed by Firestore with optional vector embeddings.

    Entries are stored in Firestore, large content in GCS.
    If an embedding function is provided (from rag.py), entries are
    stored with their vector for semantic search.
    """

    def __init__(self, user_id: str = "", embed_fn=None):
        self._user_id = _user_ref(user_id)
        self._embed_fn = embed_fn  # Optional: async fn(text) -> list[float]
        self._lock = threading.Lock()

    def _col(self):
        return _get_db().collection("users").document(self._user_id).collection("vault")

    def store(
        self,
        category: str,
        title: str,
        content: str,
        *,
        tags: Optional[List[str]] = None,
        source: str = "",
        structured_data: Optional[dict] = None,
    ) -> dict:
        category = (category or "notes").strip().lower()
        if category not in VAULT_CATEGORIES:
            category = "notes"
        title = (title or "").strip()[:200]
        content = (content or "").strip()
        if not content and not structured_data:
            return {"ok": False, "error": "Nothing to store — content is empty."}

        content_bytes = len(content.encode("utf-8", errors="replace"))
        gcs_uri = None

        # Offload large content to GCS
        if content_bytes > _VAULT_MAX_ENTRY_BYTES:
            try:
                blob_name = f"vault/{self._user_id}/{category}/{uuid.uuid4().hex}.txt"
                blob = _get_bucket().blob(blob_name)
                blob.upload_from_string(content, content_type="text/plain")
                gcs_uri = f"gs://{GCS_BUCKET}/{blob_name}"
                content = content[:2000] + f"\n\n[Full content in GCS: {gcs_uri}]"
            except Exception as e:
                print(f"[CloudVault] GCS upload failed: {e}")
                content = content[:_VAULT_MAX_ENTRY_BYTES // 2]

        entry_id = f"v_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        entry_tags = [t.strip().lower() for t in (tags or []) if t.strip()]

        entry = {
            "category": category,
            "title": title,
            "content": content,
            "tags": entry_tags,
            "source": (source or "").strip()[:300],
            "structured_data": structured_data,
            "gcs_uri": gcs_uri,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

        # Generate embedding for RAG search
        if self._embed_fn:
            try:
                embed_text = f"{title}. {content[:1500]}"
                embedding = self._embed_fn(embed_text)
                if embedding:
                    entry["embedding"] = embedding
            except Exception as e:
                print(f"[CloudVault] Embedding failed: {e}")

        with self._lock:
            # Check for near-duplicate (same category + similar title)
            try:
                existing_docs = (
                    self._col()
                    .where("category", "==", category)
                    .where("title", "==", title)
                    .limit(1)
                    .stream()
                )
                for doc in existing_docs:
                    doc.reference.set(entry, merge=True)
                    print(f"[CloudVault] Updated: [{category}] {title[:60]}")
                    return {"ok": True, "id": doc.id, "action": "updated"}
            except Exception:
                pass

            # New entry
            self._col().document(entry_id).set(entry)
            print(f"[CloudVault] Stored: [{category}] {title[:60]} ({len(content)} chars)")
            return {"ok": True, "id": entry_id, "action": "created"}

    def recall(
        self,
        query: str = "",
        category: str = "",
        tags: Optional[List[str]] = None,
        max_results: int = 10,
    ) -> list[dict]:
        """Search vault — uses vector similarity if embeddings available, else keyword."""
        # If we have an embedding function and a query, use vector search
        if query and self._embed_fn:
            try:
                return self._vector_recall(query, category, max_results)
            except Exception as e:
                print(f"[CloudVault] Vector recall failed, falling back to keyword: {e}")

        # Keyword/filter fallback
        col = self._col()
        if category:
            col = col.where("category", "==", category.strip().lower())

        results = []
        for doc in col.order_by("updated_at", direction=firestore.Query.DESCENDING).limit(max_results * 3).stream():
            entry = doc.to_dict()
            entry["id"] = doc.id

            if tags:
                tag_set = {t.strip().lower() for t in tags}
                if not tag_set.intersection(set(entry.get("tags", []))):
                    continue

            if query:
                searchable = f"{entry.get('title', '')} {entry.get('content', '')} {' '.join(entry.get('tags', []))}"
                if not any(word in searchable.lower() for word in query.lower().split()):
                    continue

            results.append(entry)
            if len(results) >= max_results:
                break

        return results

    def _vector_recall(self, query: str, category: str, max_results: int) -> list[dict]:
        """Semantic search using Firestore vector similarity."""
        query_embedding = self._embed_fn(query)
        if not query_embedding:
            return []

        from google.cloud.firestore_v1.vector import Vector
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

        col = self._col()
        vector_query = col.find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_embedding),
            distance_measure=DistanceMeasure.COSINE,
            limit=max_results * 2,
        )

        results = []
        for doc in vector_query.stream():
            entry = doc.to_dict()
            entry["id"] = doc.id
            # Remove embedding from result to keep payload small
            entry.pop("embedding", None)

            if category and entry.get("category") != category.strip().lower():
                continue

            results.append(entry)
            if len(results) >= max_results:
                break

        return results

    def delete(self, entry_id: str) -> bool:
        try:
            doc = self._col().document(entry_id).get()
            if doc.exists:
                data = doc.to_dict()
                # Clean up GCS blob if any
                gcs_uri = data.get("gcs_uri")
                if gcs_uri and gcs_uri.startswith("gs://"):
                    try:
                        blob_name = gcs_uri.split(f"gs://{GCS_BUCKET}/")[1]
                        _get_bucket().blob(blob_name).delete()
                    except Exception:
                        pass
                self._col().document(entry_id).delete()
                print(f"[CloudVault] Deleted: {entry_id}")
                return True
        except Exception:
            pass
        return False

    def list_entries(self, category: str = "", limit: int = 50) -> list[dict]:
        col = self._col()
        if category:
            col = col.where("category", "==", category.strip().lower())
        results = []
        for doc in col.order_by("updated_at", direction=firestore.Query.DESCENDING).limit(limit).stream():
            entry = doc.to_dict()
            entry["id"] = doc.id
            entry.pop("embedding", None)
            entry.pop("content", None)  # index-only: no full content
            results.append(entry)
        return results

    def get_stats(self) -> dict:
        by_cat: dict[str, int] = {}
        total = 0
        for doc in self._col().stream():
            total += 1
            cat = doc.to_dict().get("category", "notes")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        return {"total_entries": total, "by_category": by_cat}

    def to_prompt_string(self) -> str:
        stats = self.get_stats()
        if stats["total_entries"] == 0:
            return ""
        lines = [
            f"[Vault Memory — {stats['total_entries']} permanent entries across sessions]",
            "  Categories: " + ", ".join(
                f"{cat} ({count})" for cat, count in sorted(stats["by_category"].items())
            ),
        ]
        recent = self.list_entries(limit=5)
        if recent:
            lines.append("  Recent entries:")
            for entry in recent:
                cat = entry.get("category", "")
                title = entry.get("title", "(untitled)")[:80]
                lines.append(f"    - [{cat}] {title}")
            lines.append("  Use recall_memory to search or retrieve any stored entry.")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Factory — create cloud or local memory based on environment
# ═══════════════════════════════════════════════════════════════

def is_cloud() -> bool:
    """Returns True when running in a GCP environment."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("MOONWALK_CLOUD"))


def create_memory_stack(user_id: str = "", embed_fn=None):
    """
    Returns a dict of memory instances.
    Automatically picks cloud or local backends based on environment.
    """
    if is_cloud():
        uid = user_id or DEFAULT_USER_ID
        print(f"[Memory] Cloud mode — Firestore-backed for user '{uid}'")
        return {
            "conversation": CloudConversationMemory(user_id=uid),
            "profile": CloudUserProfile(user_id=uid),
            "preferences": CloudUserPreferences(user_id=uid),
            "vault": CloudVaultMemory(user_id=uid, embed_fn=embed_fn),
            "tasks": CloudTaskStore(user_id=uid),
            # WorkingMemory is always in-memory (session-scoped, no persistence needed)
        }
    else:
        # Local mode — use original file-backed classes
        from agent.memory import (
            ConversationMemory, UserProfile, UserPreferences,
            VaultMemory, TaskStore,
        )
        print("[Memory] Local mode — file-backed (~/.moonwalk/)")
        return {
            "conversation": ConversationMemory(),
            "profile": UserProfile(),
            "preferences": UserPreferences(),
            "vault": VaultMemory(),
            "tasks": TaskStore(),
        }
