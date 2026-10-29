"""
Moonwalk — Few-Shot Example Bank
==================================
Stores successful (request → plan) pairs and retrieves the most relevant
ones for LLM planning via text similarity. This acts as the agent's 
"learned experience" — each successful interaction improves future planning.

Uses lightweight TF-IDF-style similarity (no external embeddings needed)
for fast retrieval without API calls.
"""

import json
import os
import re
import math
import time
from collections import Counter
from typing import List, Optional, Tuple
from dataclasses import dataclass, field


# ── Storage ──
MOONWALK_DIR = os.path.expanduser("~/.moonwalk")
EXAMPLE_BANK_PATH = os.path.join(MOONWALK_DIR, "example_bank.json")
MAX_EXAMPLES = 200  # Cap to prevent unbounded growth
MIN_SIMILARITY = 0.25  # Minimum similarity threshold to consider a match


# ═══════════════════════════════════════════════════════════════
#  Data Structures
# ═══════════════════════════════════════════════════════════════

@dataclass
class PlanExample:
    """A stored successful (request → plan) pair."""
    request: str                   # Original user request
    intent_action: str             # e.g., "modify"
    intent_target: str             # e.g., "file"
    plan_json: dict                # The LLM-generated plan as JSON
    tools_used: List[str]          # Tools in the plan
    success: bool = True           # Whether execution succeeded
    created_at: float = 0.0
    hit_count: int = 0             # How many times this was retrieved as a match

    def to_dict(self) -> dict:
        return {
            "request": self.request,
            "intent_action": self.intent_action,
            "intent_target": self.intent_target,
            "plan_json": self.plan_json,
            "tools_used": self.tools_used,
            "success": self.success,
            "created_at": self.created_at,
            "hit_count": self.hit_count,
        }

    @staticmethod
    def from_dict(d: dict) -> "PlanExample":
        return PlanExample(
            request=d["request"],
            intent_action=d.get("intent_action", ""),
            intent_target=d.get("intent_target", ""),
            plan_json=d.get("plan_json", {}),
            tools_used=d.get("tools_used", []),
            success=d.get("success", True),
            created_at=d.get("created_at", 0.0),
            hit_count=d.get("hit_count", 0),
        )


# ═══════════════════════════════════════════════════════════════
#  Text Similarity (lightweight TF-IDF style, no external deps)
# ═══════════════════════════════════════════════════════════════

# Common stop words to filter out
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "and", "but", "or",
    "not", "no", "nor", "so", "yet", "both", "either", "neither",
    "each", "every", "all", "any", "few", "more", "most", "other",
    "some", "such", "than", "too", "very", "just", "also", "now",
    "it", "its", "i", "me", "my", "we", "our", "you", "your",
    "he", "him", "his", "she", "her", "they", "them", "their",
    "this", "that", "these", "those", "what", "which", "who", "whom",
    "how", "when", "where", "why", "if", "then", "else", "there",
    "here", "up", "out", "about", "over", "please", "go", "ahead",
})


def _tokenize(text: str) -> List[str]:
    """Tokenize text into meaningful words."""
    text = text.lower()
    # Keep file extensions and paths as single tokens
    text = re.sub(r'[^\w./~_-]', ' ', text)
    tokens = text.split()
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _compute_tf(tokens: List[str]) -> dict:
    """Compute term frequency."""
    counts = Counter(tokens)
    total = len(tokens) if tokens else 1
    return {word: count / total for word, count in counts.items()}


def cosine_similarity(tokens_a: List[str], tokens_b: List[str]) -> float:
    """Compute cosine similarity between two token lists using TF weighting."""
    if not tokens_a or not tokens_b:
        return 0.0

    tf_a = _compute_tf(tokens_a)
    tf_b = _compute_tf(tokens_b)

    # All unique terms
    all_terms = set(tf_a.keys()) | set(tf_b.keys())

    # Dot product
    dot = sum(tf_a.get(t, 0) * tf_b.get(t, 0) for t in all_terms)

    # Magnitudes
    mag_a = math.sqrt(sum(v ** 2 for v in tf_a.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in tf_b.values()))

    if mag_a == 0 or mag_b == 0:
        return 0.0

    return dot / (mag_a * mag_b)


# ═══════════════════════════════════════════════════════════════
#  Example Bank
# ═══════════════════════════════════════════════════════════════

class ExampleBank:
    """
    Persistent bank of successful plan examples.
    
    Usage:
        bank = ExampleBank()
        
        # After a successful plan execution:
        bank.record(request, intent_action, intent_target, plan_dict, tools)
        
        # Before LLM planning (get similar examples):
        examples = bank.retrieve(user_request, top_k=3)
    """

    def __init__(self):
        os.makedirs(MOONWALK_DIR, exist_ok=True)
        self._examples: List[PlanExample] = self._load()
        # Pre-tokenize for fast similarity search
        self._token_cache: dict[int, List[str]] = {}
        self._rebuild_token_cache()

    def _load(self) -> List[PlanExample]:
        if os.path.exists(EXAMPLE_BANK_PATH):
            try:
                with open(EXAMPLE_BANK_PATH, "r") as f:
                    data = json.load(f)
                return [PlanExample.from_dict(d) for d in data]
            except Exception:
                return []
        return []

    def _save(self):
        try:
            with open(EXAMPLE_BANK_PATH, "w") as f:
                json.dump([e.to_dict() for e in self._examples], f, indent=2)
        except Exception:
            pass

    def _rebuild_token_cache(self):
        """Rebuild the token cache for all examples."""
        self._token_cache = {}
        for i, ex in enumerate(self._examples):
            self._token_cache[i] = _tokenize(ex.request)

    def record(
        self,
        request: str,
        intent_action: str,
        intent_target: str,
        plan_json: dict,
        tools_used: List[str],
        success: bool = True,
    ):
        """
        Record a successful (request → plan) pair.
        
        Deduplicates by checking if a very similar request already exists.
        If so, updates the existing example's hit count instead.
        """
        # Check for near-duplicate
        request_tokens = _tokenize(request)
        for i, ex in enumerate(self._examples):
            cached_tokens = self._token_cache.get(i, _tokenize(ex.request))
            sim = cosine_similarity(request_tokens, cached_tokens)
            if sim > 0.85:
                # Near-duplicate: update hit count and plan if newer
                ex.hit_count += 1
                if success and not ex.success:
                    ex.plan_json = plan_json
                    ex.tools_used = tools_used
                    ex.success = success
                self._save()
                return

        # New example
        example = PlanExample(
            request=request,
            intent_action=intent_action,
            intent_target=intent_target,
            plan_json=plan_json,
            tools_used=tools_used,
            success=success,
            created_at=time.time(),
            hit_count=0,
        )
        self._examples.append(example)
        self._token_cache[len(self._examples) - 1] = request_tokens

        # Evict oldest low-hit examples if over capacity
        if len(self._examples) > MAX_EXAMPLES:
            # Sort by (hit_count, created_at) and remove the least useful
            scored = [(i, ex.hit_count, ex.created_at) for i, ex in enumerate(self._examples)]
            scored.sort(key=lambda x: (x[1], x[2]))
            remove_idx = scored[0][0]
            self._examples.pop(remove_idx)
            self._rebuild_token_cache()

        self._save()

    def retrieve(
        self,
        request: str,
        intent_action: Optional[str] = None,
        intent_target: Optional[str] = None,
        top_k: int = 3,
    ) -> List[Tuple[PlanExample, float]]:
        """
        Retrieve the top-k most similar examples to the given request.
        
        Args:
            request: The user's request text
            intent_action: Optional filter by intent action
            intent_target: Optional filter by intent target
            top_k: Number of examples to return
            
        Returns:
            List of (example, similarity_score) tuples, sorted by similarity desc.
        """
        if not self._examples:
            return []

        request_tokens = _tokenize(request)
        scores: List[Tuple[int, float]] = []

        for i, ex in enumerate(self._examples):
            # Optional intent filtering — boost score for same intent
            intent_boost = 0.0
            if intent_action and ex.intent_action == intent_action:
                intent_boost += 0.1
            if intent_target and ex.intent_target == intent_target:
                intent_boost += 0.05

            cached_tokens = self._token_cache.get(i, _tokenize(ex.request))
            sim = cosine_similarity(request_tokens, cached_tokens) + intent_boost

            if sim >= MIN_SIMILARITY:
                scores.append((i, min(sim, 1.0)))

        # Sort by similarity descending
        scores.sort(key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in scores[:top_k]:
            ex = self._examples[idx]
            ex.hit_count += 1  # Track retrieval hits
            results.append((ex, score))

        if results:
            self._save()  # Persist hit count updates

        return results

    def format_for_prompt(
        self,
        request: str,
        intent_action: Optional[str] = None,
        intent_target: Optional[str] = None,
        top_k: int = 2,
    ) -> str:
        """
        Retrieve similar examples and format them for injection into the LLM prompt.
        
        Returns empty string if no relevant examples found.
        """
        results = self.retrieve(request, intent_action, intent_target, top_k)
        if not results:
            return ""

        lines = ["\n## Similar Past Requests (learned from experience — use as guidance)"]
        for ex, score in results:
            plan = ex.plan_json
            plan_str = json.dumps(plan, indent=2)
            # Truncate very long plans
            if len(plan_str) > 500:
                plan_str = plan_str[:500] + "..."
            lines.append(f'\nRequest: "{ex.request}"')
            lines.append(plan_str)

        return "\n".join(lines)

    @property
    def size(self) -> int:
        return len(self._examples)
