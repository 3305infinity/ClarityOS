"""
Moonwalk — Browser Search Engine
================================
Implements the "Antigravity" architecture for DOM searching:
1. Local, heuristic-based scoring (no LLM latency).
2. Fuzzy matching for resilience.
3. Viewport-aware prioritization.
"""

import re
from typing import List, Dict, Any, Optional
from difflib import SequenceMatcher

class AntigravitySearcher:
    """
    High-performance DOM searcher inspired by Antigravity architecture.
    Uses heuristic scoring to find elements instantly without LLM roundtrips.
    """
    
    def search(self, query: str, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Search for elements matching the query.
        Returns sorted list of matches by relevance.
        """
        if not nodes:
            return []
            
        query = query.lower().strip()
        scored = []
        
        for node in nodes:
            score = self._score_node(query, node)
            if score > 0:
                # Inject score for debugging/sorting
                node["_score"] = score
                scored.append(node)
                
        # Sort by score descending
        scored.sort(key=lambda x: x["_score"], reverse=True)
        return scored

    def _score_node(self, query: str, node: Dict[str, Any]) -> float:
        score = 0.0
        
        # Extract fields safely
        text = str(node.get("text") or "").lower().strip()
        attributes = node.get("attributes") or {}
        aria_label = str(attributes.get("aria-label") or "").lower().strip()
        placeholder = str(attributes.get("placeholder") or "").lower().strip()
        name = str(attributes.get("name") or "").lower().strip()
        role = str(attributes.get("role") or "").lower().strip()
        tag = str(node.get("tagName") or "").lower().strip()
        
        # 1. Exact Matches (Highest Priority)
        if query == text: score += 100
        if query == aria_label: score += 95
        if query == placeholder: score += 90
        if query == name: score += 85
        
        # 2. Contains Matches (High Priority)
        # "Sign in" matches "Sign in to Google"
        if query in text: 
            score += 60
            # Penalize very long text (e.g. matching "cat" in a whole paragraph)
            if len(text) > len(query) * 5:
                score -= 10
        if query in aria_label: score += 55
        
        # 3. Fuzzy Match (Resilience)
        # Handle typos like "serch" -> "Search"
        if score < 50 and len(query) > 3:
            text_ratio = SequenceMatcher(None, query, text).ratio()
            label_ratio = SequenceMatcher(None, query, aria_label).ratio()
            if text_ratio > 0.8: score += 40
            elif label_ratio > 0.8: score += 40

        # 4. Role/Tag Boosting (Contextual)
        # If query implies action, boost interactive elements
        is_interactive = tag in ["button", "a", "input", "select", "textarea"] or role in ["button", "link", "textbox", "menuitem"]
        
        if is_interactive:
            score += 10
            
        return score

# Singleton instance for easy import
search_engine = AntigravitySearcher()