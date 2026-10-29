"""
Moonwalk — RAG Engine (Retrieval-Augmented Generation)
=======================================================
Adds semantic memory to the agent using Vertex AI text embeddings
and Firestore vector search.

Three retrieval modes:
  1. **Vault RAG** — Semantic search over the permanent vault
  2. **Session RAG** — Recall relevant past conversations
  3. **Contextual RAG** — Augment the current prompt with relevant knowledge

Usage:
  rag = get_rag_engine()
  
  # Embed text for storage
  vector = rag.embed("some text")
  
  # Augment a prompt with relevant vault knowledge
  augmented = await rag.augment_prompt("user query", vault_memory)
"""

from __future__ import annotations

import os
import time
import json
import hashlib
from typing import Optional, List, Any
from functools import lru_cache

# Google Cloud AI Platform
from google import genai  # type: ignore[import]

# ── Configuration ──

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("MOONWALK_EMBEDDING_MODEL", "text-embedding-004")
EMBEDDING_DIMENSIONS = 768  # text-embedding-004 default

# Cache for computed embeddings (in-memory LRU)
_EMBED_CACHE_SIZE = 500


# ═══════════════════════════════════════════════════════════════
#  RAG Engine
# ═══════════════════════════════════════════════════════════════

class RAGEngine:
    """
    Retrieval-Augmented Generation engine for Moonwalk.
    
    Uses Gemini's text-embedding-004 model for embeddings and
    Firestore vector search for retrieval.
    """

    def __init__(self):
        self._client = genai.Client(api_key=GEMINI_API_KEY)
        self._embed_cache: dict[str, list[float]] = {}
        print(f"[RAG] Initialized with model: {EMBEDDING_MODEL}")

    def embed(self, text: str) -> list[float]:
        """
        Generate an embedding vector for the given text.
        Uses a local cache to avoid re-computing identical texts.
        
        Returns a list of floats (the embedding vector).
        """
        if not text or not text.strip():
            return []

        # Check cache
        cache_key = hashlib.md5(text[:2000].encode()).hexdigest()
        if cache_key in self._embed_cache:
            return self._embed_cache[cache_key]

        try:
            result = self._client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text[:2000],  # Limit input length
            )
            
            embedding = result.embeddings[0].values
            
            # Cache it
            if len(self._embed_cache) >= _EMBED_CACHE_SIZE:
                # Evict oldest (FIFO)
                oldest = next(iter(self._embed_cache))
                del self._embed_cache[oldest]
            self._embed_cache[cache_key] = embedding

            return embedding

        except Exception as e:
            print(f"[RAG] Embedding failed: {e}")
            return []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts in a single API call.
        More efficient than calling embed() in a loop.
        """
        if not texts:
            return []

        try:
            result = self._client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=[t[:2000] for t in texts],
            )
            return [e.values for e in result.embeddings]
        except Exception as e:
            print(f"[RAG] Batch embedding failed: {e}")
            return [[] for _ in texts]

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def augment_prompt(
        self,
        query: str,
        vault_results: list[dict],
        max_context_chars: int = 3000,
    ) -> str:
        """
        Build a RAG context block from retrieved vault entries.
        
        Args:
            query: The user's query
            vault_results: Pre-retrieved vault entries (from CloudVaultMemory.recall)
            max_context_chars: Maximum characters for the RAG context
            
        Returns:
            A formatted string to inject into the system prompt
        """
        if not vault_results:
            return ""

        lines = [
            "[Relevant Knowledge — retrieved from your permanent memory vault]",
        ]
        total_chars = 0

        for entry in vault_results:
            title = entry.get("title", "")
            content = entry.get("content", "")
            category = entry.get("category", "")
            tags = ", ".join(entry.get("tags", []))

            # Build entry block
            block = f"\n  [{category}] {title}"
            if tags:
                block += f"  (tags: {tags})"
            if content:
                remaining = max_context_chars - total_chars - len(block) - 50
                if remaining < 100:
                    break
                block += f"\n  {content[:remaining]}"

            lines.append(block)
            total_chars += len(block)

            if total_chars >= max_context_chars:
                break

        if len(lines) == 1:
            return ""

        lines.append("")
        return "\n".join(lines)

    def build_session_context(
        self,
        query: str,
        past_sessions: list[dict],
        max_results: int = 3,
    ) -> str:
        """
        Find and format relevant past session summaries for the current query.
        Uses embedding similarity to rank past sessions.
        
        Args:
            query: Current user query
            past_sessions: List of session dicts with 'summary' and 'turns' fields
            max_results: Maximum number of past sessions to include
            
        Returns:
            Formatted context string for the system prompt
        """
        if not past_sessions or not query:
            return ""

        query_emb = self.embed(query)
        if not query_emb:
            return ""

        scored = []
        for session in past_sessions:
            summary = session.get("summary", "")
            if not summary:
                # Build summary from first/last turn
                turns = session.get("turns", [])
                if turns:
                    first_text = turns[0].get("parts", [{}])[0].get("text", "")
                    summary = first_text[:200]
            if not summary:
                continue

            session_emb = self.embed(summary)
            if session_emb:
                sim = self.cosine_similarity(query_emb, session_emb)
                scored.append((sim, session, summary))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:max_results]

        if not top or top[0][0] < 0.3:  # similarity threshold
            return ""

        lines = ["[Relevant Past Sessions]"]
        for sim, session, summary in top:
            lines.append(f"  - (relevance: {sim:.0%}) {summary[:300]}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  Singleton
# ═══════════════════════════════════════════════════════════════

_rag_engine: Optional[RAGEngine] = None


def get_rag_engine() -> RAGEngine:
    """Get or create the singleton RAG engine."""
    global _rag_engine
    if _rag_engine is None:
        _rag_engine = RAGEngine()
    return _rag_engine


def get_embed_fn():
    """
    Returns a synchronous embedding function suitable for
    passing to CloudVaultMemory(embed_fn=...).
    """
    engine = get_rag_engine()
    return engine.embed
