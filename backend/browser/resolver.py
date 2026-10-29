"""
Moonwalk — Browser Resolver
===========================
Deterministic candidate ranking for stable browser ref selection.
"""

from typing import Dict, List, Optional, Tuple
from .models import ElementRef


class BrowserResolver:
    INTERACTIVE_ROLES = {
        "button", "link", "textbox", "searchbox", "combobox", "checkbox",
        "radio", "menuitem", "tab", "option", "switch"
    }

    FIELD_TERMS = {
        "field", "input", "textbox", "text box", "searchbox",
        "search box", "searchbar", "search bar", "search field", "box", "entry", "combobox",
        "drop down", "dropdown"
    }
    BUTTON_TERMS = {
        "button", "submit", "continue", "save", "cancel", "next", "back",
        "search button", "icon button"
    }
    LINK_TERMS = {"link", "url", "href"}

    FIELD_ROLES = {"textbox", "searchbox", "combobox", "input", "textarea", "select"}
    BUTTON_ROLES = {"button", "menuitem", "tab", "switch"}
    LINK_ROLES = {"link", "a"}

    def resolve(
        self,
        query: str,
        elements: List[ElementRef],
        action: str = "click",
        limit: int = 10,
    ) -> List[Tuple[float, ElementRef, Dict[str, int]]]:
        query_norm = self._norm(query)
        query_tokens = set(t for t in query_norm.split() if t)
        intent_hints = self._infer_intent_hints(query_norm, action)
        ranked: List[Tuple[float, ElementRef, Dict[str, int]]] = []

        for element in elements:
            if not element.visible or not element.enabled:
                continue
            if action and not element.supports(action):
                continue

            score_breakdown = {
                "label": 0,
                "context": 0,
                "role": 0,
                "attribute": 0,
                "semantic": 0,
            }
            labels = [
                element.text,
                element.aria_label,
                element.name,
                element.placeholder,
                element.href,
                element.context_text,
            ]
            labels_norm = [self._norm(v) for v in labels if v]
            joined = " ".join(labels_norm)

            if query_norm and any(label == query_norm for label in labels_norm):
                score_breakdown["label"] += 120
            if query_norm and query_norm in joined:
                score_breakdown["label"] += 70

            if query_tokens:
                label_tokens = set(joined.split())
                overlap = query_tokens & label_tokens
                score_breakdown["context"] += len(overlap) * 12

            role_norm = self._norm(element.role or element.tag)
            if action == "click" and role_norm in self.INTERACTIVE_ROLES:
                score_breakdown["role"] += 20
            if action in ("type", "fill") and role_norm in {"textbox", "searchbox", "combobox", "input", "textarea"}:
                score_breakdown["role"] += 35
            if action == "select" and role_norm in {"combobox", "listbox", "option", "select"}:
                score_breakdown["role"] += 35

            score_breakdown["semantic"] += self._semantic_role_score(
                query_norm=query_norm,
                action=action,
                role_norm=role_norm,
                labels_norm=labels_norm,
                intent_hints=intent_hints,
            )

            fingerprint = element.fingerprint
            attr_values = [
                fingerprint.text,
                fingerprint.aria_label,
                fingerprint.name,
                fingerprint.placeholder,
                fingerprint.href,
                " ".join(fingerprint.ancestor_labels),
            ]
            attr_norm = " ".join(self._norm(v) for v in attr_values if v)
            if query_norm and query_norm in attr_norm:
                score_breakdown["attribute"] += 40

            # Viewport awareness — boost visible-on-screen elements
            if getattr(element, 'in_viewport', True):
                score_breakdown["semantic"] += 8

            total = float(sum(score_breakdown.values()))
            if total > 0:
                ranked.append((total, element, score_breakdown))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[:limit]

    def describe_candidates(
        self,
        query: str,
        elements: List[ElementRef],
        action: str = "click",
        limit: int = 5,
    ) -> List[Dict[str, object]]:
        candidates = []
        for score, element, breakdown in self.resolve(query, elements, action=action, limit=limit):
            candidates.append({
                "ref_id": element.ref_id,
                "agent_id": element.agent_id,
                "label": element.primary_label(),
                "role": element.role or element.tag,
                "tag": element.tag,
                "score": round(score, 2),
                "visible": element.visible,
                "enabled": element.enabled,
                "actions": element.action_types,
                "context": element.context_text,
                "text": element.text,
                "aria_label": element.aria_label,
                "name": element.name,
                "placeholder": element.placeholder,
                "breakdown": breakdown,
                "generation": element.generation,
            })
        return candidates

    def best_candidate(
        self,
        query: str,
        elements: List[ElementRef],
        action: str = "click",
    ) -> Optional[ElementRef]:
        matches = self.resolve(query, elements, action=action, limit=1)
        return matches[0][1] if matches else None

    def _norm(self, text: str) -> str:
        return " ".join((text or "").strip().lower().split())

    def _infer_intent_hints(self, query_norm: str, action: str) -> Dict[str, bool]:
        hints = {
            "wants_field": False,
            "wants_button": False,
            "wants_link": False,
            "wants_search": False,
        }
        compact = query_norm.replace("-", " ")
        hints["wants_field"] = action in {"type", "fill", "select"} or any(term in compact for term in self.FIELD_TERMS)
        hints["wants_button"] = any(term in compact for term in self.BUTTON_TERMS)
        hints["wants_link"] = any(term in compact for term in self.LINK_TERMS)
        hints["wants_search"] = "search" in compact
        if hints["wants_button"] and action == "click":
            hints["wants_field"] = False
        return hints

    def _semantic_role_score(
        self,
        query_norm: str,
        action: str,
        role_norm: str,
        labels_norm: List[str],
        intent_hints: Dict[str, bool],
    ) -> int:
        score = 0
        label_text = " ".join(labels_norm)

        if intent_hints["wants_field"]:
            if role_norm in self.FIELD_ROLES:
                score += 90
            elif role_norm in self.BUTTON_ROLES and action == "click":
                score -= 35

        if intent_hints["wants_button"]:
            if role_norm in self.BUTTON_ROLES:
                score += 70
            elif role_norm in self.FIELD_ROLES:
                score -= 15

        if intent_hints["wants_link"]:
            if role_norm in self.LINK_ROLES:
                score += 70
            elif role_norm in self.BUTTON_ROLES:
                score -= 10

        if intent_hints["wants_search"]:
            if "search" in label_text:
                score += 25
            if role_norm in {"searchbox", "combobox"}:
                score += 30

        if action == "click" and intent_hints["wants_field"] and role_norm in self.FIELD_ROLES:
            score += 30

        return score
