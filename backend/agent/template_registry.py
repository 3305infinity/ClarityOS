"""
Moonwalk — JSON Template Registry
=================================
Loads optional template packs from backend/agent/templates/packs/*.json
and surfaces them as advisory skill overlays for the milestone planner.
"""

import glob
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional
from functools import partial
from urllib.parse import urlparse

from agent.world_state import UserIntent, WorldState

print = partial(print, flush=True)


@dataclass
class PackSkill:
    """Optional skill-like metadata used for semantic + capability matching."""

    name: str
    description: str = ""
    examples: list[str] = field(default_factory=list)
    capabilities_any: list[str] = field(default_factory=list)
    capabilities_all: list[str] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    min_semantic_score: float = 0.0
    semantic_weight: float = 9.0
    _token_cache: list[list[str]] = field(default_factory=list, repr=False)

    def build_cache(self) -> None:
        self._token_cache.clear()
        if self.description:
            self._token_cache.append(_tokenize_text(self.description))
        for example in self.examples:
            self._token_cache.append(_tokenize_text(example))

    def semantic_score(self, request_tokens: list[str]) -> float:
        if not self._token_cache:
            self.build_cache()
        if not request_tokens or not self._token_cache:
            return 0.0
        best = 0.0
        for cached in self._token_cache:
            sim = _cosine_similarity(request_tokens, cached)
            if sim > best:
                best = sim
        return best


@dataclass
class TemplatePack:
    name: str
    priority: int
    match: dict
    plan: dict
    constraints: dict
    final_response: str
    source_file: str
    enabled: bool = True
    version: str = "1"
    tags: list[str] = field(default_factory=list)
    skill: Optional[PackSkill] = None


@dataclass
class PackCandidate:
    pack: TemplatePack
    captures: dict
    score: float
    reasons: list[str]


_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "and", "but", "or",
    "not", "no", "nor", "so", "yet", "both", "either", "neither", "each",
    "every", "all", "any", "few", "more", "most", "other", "some", "such",
    "than", "too", "very", "just", "also", "now", "it", "its", "i", "me",
    "my", "we", "our", "you", "your", "he", "him", "his", "she", "her",
    "they", "them", "their", "this", "that", "these", "those", "what",
    "which", "who", "whom", "how", "when", "where", "why", "if", "then",
    "else", "there", "here", "up", "out", "about", "over", "please",
})


def _tokenize_text(text: str) -> list[str]:
    if not text:
        return []
    lowered = text.lower()
    lowered = re.sub(r"[^\w./~_-]", " ", lowered)
    tokens = lowered.split()
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _cosine_similarity(tokens_a: list[str], tokens_b: list[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    counter_a = Counter(tokens_a)
    counter_b = Counter(tokens_b)
    terms = set(counter_a) | set(counter_b)
    dot = sum(counter_a.get(t, 0) * counter_b.get(t, 0) for t in terms)
    mag_a = math.sqrt(sum(v * v for v in counter_a.values()))
    mag_b = math.sqrt(sum(v * v for v in counter_b.values()))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


class TemplateRegistry:
    """Loads and evaluates JSON-backed planner templates."""

    def __init__(self, packs_dir: Optional[str] = None):
        if packs_dir is None:
            packs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "packs")
        self.packs_dir = packs_dir
        self._packs: list[TemplatePack] = []
        self._stats: dict[str, Any] = {
            "requests": 0,
            "matched": 0,
            "no_match": 0,
            "pack_hits": {},
            "skip_reasons": {},
            "last_top_candidates": [],
        }
        self._load_packs()

    @property
    def size(self) -> int:
        return len(self._packs)

    def stats_snapshot(self) -> dict:
        return {
            "requests": self._stats.get("requests", 0),
            "matched": self._stats.get("matched", 0),
            "no_match": self._stats.get("no_match", 0),
            "pack_hits": dict(self._stats.get("pack_hits", {})),
            "skip_reasons": dict(self._stats.get("skip_reasons", {})),
            "last_top_candidates": list(self._stats.get("last_top_candidates", [])),
        }

    def _derive_search_query(self, user_request: str, intent: Optional[UserIntent] = None) -> str:
        """
        Legacy helper retained for compatibility tests.

        The registry no longer drives direct template execution, but tests still
        rely on the old query-normalization behavior for noisy research prompts.
        """
        text = (user_request or "").lower()
        if not text and intent and getattr(intent, "target_value", None):
            return str(intent.target_value).strip().lower()

        text = re.sub(r"^[\s,]*(can you|could you|please|hey moonwalk|moonwalk)\s+", "", text)
        text = re.sub(r"^[\s,]*(research|investigate|study|analy[sz]e|compare|find me)\s+", "", text)
        text = re.sub(r"\b(create|write|make)\b.*$", "", text)
        text = re.sub(r"\b(a|an|the)\b", " ", text)
        text = re.sub(r"\b(detailed|detail|google|document|doc|report|about it|if possible)\b", " ", text)
        text = re.sub(r"\band\s*$", "", text)
        text = re.sub(r"\s+", " ", text).strip(" ?.!,:;")

        if text:
            return text
        if intent and getattr(intent, "target_value", None):
            return str(intent.target_value).strip().lower()
        return (user_request or "").strip().lower()

    def _bump_stat(self, field: str, key: Optional[str] = None) -> None:
        if key is None:
            self._stats[field] = int(self._stats.get(field, 0)) + 1
            return
        bucket = self._stats.setdefault(field, {})
        bucket[key] = int(bucket.get(key, 0)) + 1

    def _record_top_candidates(self, candidates: list[PackCandidate]) -> None:
        top = []
        for candidate in candidates[:3]:
            top.append({
                "name": candidate.pack.name,
                "score": round(float(candidate.score), 2),
                "reasons": list(candidate.reasons[:4]),
            })
        self._stats["last_top_candidates"] = top

    def _load_packs(self) -> None:
        self._packs.clear()
        if not os.path.isdir(self.packs_dir):
            print(f"[TemplateRegistry] Pack directory missing: {self.packs_dir}")
            return

        paths = sorted(glob.glob(os.path.join(self.packs_dir, "*.json")))
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print(f"[TemplateRegistry] Invalid JSON ({os.path.basename(path)}): {e}")
                continue

            entries = raw if isinstance(raw, list) else [raw]
            for entry in entries:
                pack, error = self._validate_entry(entry, path)
                if not pack:
                    print(f"[TemplateRegistry] Skipping pack in {os.path.basename(path)}: {error}")
                    continue
                if not pack.enabled:
                    print(f"[TemplateRegistry] Pack '{pack.name}' disabled; skipping")
                    continue
                self._packs.append(pack)

        self._packs.sort(key=lambda p: p.priority, reverse=True)
        print(f"[TemplateRegistry] Loaded {len(self._packs)} pack(s) from {self.packs_dir}")

    def _validate_entry(self, entry: Any, source_file: str) -> tuple[Optional[TemplatePack], str]:
        if not isinstance(entry, dict):
            return None, "entry must be an object"

        required_keys = ("name", "priority", "match", "plan", "constraints", "final_response")
        missing = [k for k in required_keys if k not in entry]
        if missing:
            return None, f"missing keys: {', '.join(missing)}"

        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            return None, "name must be a non-empty string"
        if not isinstance(entry.get("priority"), int):
            return None, "priority must be an integer"
        if not isinstance(entry.get("match"), dict):
            return None, "match must be an object"
        if not isinstance(entry.get("plan"), dict):
            return None, "plan must be an object"
        if not isinstance(entry.get("constraints"), dict):
            return None, "constraints must be an object"
        if not isinstance(entry.get("final_response"), str):
            return None, "final_response must be a string"
        if "enabled" in entry and not isinstance(entry.get("enabled"), bool):
            return None, "enabled must be a boolean when provided"
        if "version" in entry and not isinstance(entry.get("version"), str):
            return None, "version must be a string when provided"
        if "tags" in entry:
            tags = entry.get("tags")
            if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
                return None, "tags must be an array of strings when provided"
        skill, skill_error = self._parse_skill(entry.get("skill"), entry["name"])
        if skill_error:
            return None, skill_error

        plan = entry["plan"]
        if not isinstance(plan.get("steps"), list) or not plan.get("steps"):
            return None, "plan.steps must be a non-empty array"

        required_tools = entry["constraints"].get("required_tools")
        if not isinstance(required_tools, list):
            return None, "constraints.required_tools must be an array"
        forbidden_tools = entry["constraints"].get("forbidden_tools", [])
        if not isinstance(forbidden_tools, list):
            return None, "constraints.forbidden_tools must be an array when provided"

        min_steps = entry["constraints"].get("min_steps")
        max_steps = entry["constraints"].get("max_steps")
        if min_steps is not None and (not isinstance(min_steps, int) or min_steps < 0):
            return None, "constraints.min_steps must be a non-negative integer when provided"
        if max_steps is not None and (not isinstance(max_steps, int) or max_steps < 0):
            return None, "constraints.max_steps must be a non-negative integer when provided"
        if min_steps is not None and max_steps is not None and min_steps > max_steps:
            return None, "constraints.min_steps cannot be greater than constraints.max_steps"

        for i, step in enumerate(plan["steps"], start=1):
            if not isinstance(step, dict):
                return None, f"plan.steps[{i}] must be an object"
            if not isinstance(step.get("description"), str) or not step["description"].strip():
                return None, f"plan.steps[{i}].description must be a non-empty string"
            if not isinstance(step.get("tool"), str) or not step["tool"].strip():
                return None, f"plan.steps[{i}].tool must be a non-empty string"
            if not isinstance(step.get("args"), dict):
                return None, f"plan.steps[{i}].args must be an object"

        return TemplatePack(
            name=entry["name"].strip(),
            priority=entry["priority"],
            match=entry["match"],
            plan=entry["plan"],
            constraints=entry["constraints"],
            final_response=entry["final_response"],
            source_file=source_file,
            enabled=entry.get("enabled", True),
            version=entry.get("version", "1"),
            tags=list(entry.get("tags") or []),
            skill=skill,
        ), ""

    def _parse_skill(self, skill_cfg: Any, pack_name: str) -> tuple[Optional[PackSkill], str]:
        if skill_cfg is None:
            return None, ""
        if not isinstance(skill_cfg, dict):
            return None, "skill must be an object when provided"

        name = str(skill_cfg.get("name") or pack_name).strip()
        description = str(skill_cfg.get("description") or "").strip()

        examples_raw = skill_cfg.get("examples") or skill_cfg.get("example_phrases") or []
        if isinstance(examples_raw, str):
            examples_raw = [examples_raw]
        if not isinstance(examples_raw, list) or any(not isinstance(e, str) for e in examples_raw):
            return None, "skill.examples must be an array of strings when provided"
        examples = [e.strip() for e in examples_raw if str(e).strip()]

        capabilities_any = self._norm_list(skill_cfg.get("capabilities_any"))
        capabilities_all = self._norm_list(skill_cfg.get("capabilities_all"))
        anti_patterns = self._norm_list(skill_cfg.get("anti_patterns"))

        min_semantic_score = skill_cfg.get("min_semantic_score", 0.0)
        semantic_weight = skill_cfg.get("semantic_weight", 9.0)
        try:
            min_semantic_score = float(min_semantic_score)
            semantic_weight = float(semantic_weight)
        except (TypeError, ValueError):
            return None, "skill.min_semantic_score and skill.semantic_weight must be numeric"
        if min_semantic_score < 0.0 or min_semantic_score > 1.0:
            return None, "skill.min_semantic_score must be between 0 and 1"
        if semantic_weight < 0.0:
            return None, "skill.semantic_weight must be >= 0"

        skill = PackSkill(
            name=name,
            description=description,
            examples=examples,
            capabilities_any=capabilities_any,
            capabilities_all=capabilities_all,
            anti_patterns=anti_patterns,
            min_semantic_score=min_semantic_score,
            semantic_weight=semantic_weight,
        )
        skill.build_cache()
        return skill, ""

    def try_match(
        self,
        user_request: str,
        intent: UserIntent,
        world_state: WorldState,
        available_tools: Optional[list[str]] = None,
    ) -> tuple[None, None]:
        """Legacy compatibility hook.

        Direct template-to-plan execution has been retired. Packs now only
        contribute advisory skill context through `get_skill_candidates()`.
        """
        print("[TemplateRegistry] Direct template execution disabled; using advisory skill overlays only")
        return None, None

    def get_skill_candidates(
        self,
        user_request: str,
        intent: UserIntent,
        world_state: WorldState,
        available_tools: Optional[list[str]] = None,
        limit: int = 3,
    ) -> list[PackCandidate]:
        text = (user_request or "").strip()
        if not text:
            return []

        candidates = self._rank_candidates(text, intent, world_state)
        filtered: list[PackCandidate] = []
        allowed = set(available_tools or [])
        for candidate in candidates:
            pack = candidate.pack
            if available_tools:
                required_tools = set(pack.constraints.get("required_tools") or [])
                if required_tools and not required_tools.issubset(allowed):
                    continue
            filtered.append(candidate)
            if len(filtered) >= max(1, limit):
                break
        return filtered

    def format_skill_context(self, candidates: list[PackCandidate], limit: int = 3) -> str:
        if not candidates:
            return "(none)"

        lines: list[str] = []
        for idx, candidate in enumerate(candidates[: max(1, limit)], start=1):
            pack = candidate.pack
            skill = pack.skill
            skill_name = skill.name if skill and skill.name else pack.name
            description = ""
            if skill and skill.description:
                description = skill.description.strip()
            elif pack.tags:
                description = f"Tags: {', '.join(pack.tags[:4])}"
            else:
                description = f"Optional pack '{pack.name}' matched this request."

            lines.append(f"{idx}. {skill_name} — {description}")

            if skill and skill.examples:
                lines.append(f"   Example: {skill.examples[0][:120]}")

            required_tools = list(pack.constraints.get('required_tools') or [])
            if required_tools:
                lines.append(f"   Suggested tools: {', '.join(required_tools[:6])}")

            plan_tools: list[str] = []
            for step in pack.plan.get("steps", []):
                tool_name = str(step.get("tool", "")).strip()
                if tool_name and tool_name not in plan_tools:
                    plan_tools.append(tool_name)
            if plan_tools:
                lines.append(f"   Typical flow: {', '.join(plan_tools[:6])}")

            if candidate.reasons:
                lines.append(f"   Match reasons: {', '.join(candidate.reasons[:4])}")

        return "\n".join(lines)

    def skill_names(self, candidates: list[PackCandidate]) -> list[str]:
        names: list[str] = []
        for candidate in candidates:
            skill = candidate.pack.skill
            if skill and skill.name:
                names.append(skill.name)
            else:
                names.append(candidate.pack.name)
        return names

    def _rank_candidates(
        self,
        user_request: str,
        intent: UserIntent,
        world_state: WorldState,
    ) -> list[PackCandidate]:
        request_tokens = _tokenize_text(user_request)
        request_capabilities = self._derive_request_capabilities(user_request, intent, world_state)
        candidates: list[PackCandidate] = []
        for pack in self._packs:
            matched = self._match_pack(
                pack,
                user_request,
                intent,
                world_state,
                request_tokens=request_tokens,
                request_capabilities=request_capabilities,
            )
            if matched is None:
                continue
            captures, score, reasons = matched
            candidates.append(PackCandidate(pack=pack, captures=captures, score=score, reasons=reasons))

        candidates.sort(key=lambda c: (c.score, c.pack.priority), reverse=True)
        return candidates

    def _norm_list(self, value: Any, *, lower: bool = True) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            normalized.append(text.lower() if lower else text)
        return normalized

    def _extract_domain(self, url: str) -> str:
        if not url:
            return ""
        try:
            return (urlparse(url).netloc or "").lower()
        except Exception:
            return ""

    def _derive_request_capabilities(
        self,
        user_request: str,
        intent: UserIntent,
        world_state: WorldState,
    ) -> set[str]:
        text = (user_request or "").lower()
        capabilities: set[str] = set()

        if any(term in text for term in ("research", "investigate", "study", "find", "look up", "analyse", "analyze")):
            capabilities.add("research")
        if any(term in text for term in ("compare", "versus", "vs", "rank", "ranking")):
            capabilities.add("comparison")
        if any(term in text for term in ("document", "google doc", "google document", "report", "brief", "write up", "write-up")):
            capabilities.add("document_output")
        if re.search(r"\b(save|write)\b.*\b(to|into|as)\b", text) or re.search(r"\b[\w./~-]+\.(md|txt|docx?|rtf|json|yaml|yml)\b", text):
            capabilities.add("file_output")
        if any(term in text for term in ("replace", "patch", "edit", "update", "modify", "rename")):
            capabilities.add("file_edit")
        if any(term in text for term in ("fill", "form", "submit", "field", "dropdown", "checkbox", "radio button")):
            capabilities.add("form_fill")
        if any(term in text for term in ("click", "select", "type", "scroll", "tab", "button", "link")):
            capabilities.add("browser_action")
        if any(term in text for term in ("website", "page", "article", "browser", "web")):
            capabilities.add("browser_read")
        if world_state.browser_url:
            capabilities.add("browser_context")

        action = intent.action.value
        target = intent.target_type.value
        if action in {"search", "analyze", "query"}:
            capabilities.add("research")
            capabilities.add("browser_read")
        if action in {"modify", "create"} and target == "file":
            capabilities.add("file_edit")
        if action == "create" and ("document" in text or "doc" in text):
            capabilities.add("document_output")
        if action in {"open", "close", "navigate"}:
            capabilities.add("app_control")
        if target == "agent" or "agent" in text:
            capabilities.add("agent_control")

        return capabilities

    def _match_skill(
        self,
        skill: Optional[PackSkill],
        *,
        text: str,
        request_tokens: list[str],
        request_capabilities: set[str],
    ) -> Optional[tuple[float, list[str]]]:
        if skill is None:
            return 0.0, []

        reasons: list[str] = []
        score = 0.0

        if skill.anti_patterns and any(pattern in text for pattern in skill.anti_patterns):
            return None

        if skill.capabilities_all and not all(cap in request_capabilities for cap in skill.capabilities_all):
            return None
        if skill.capabilities_all:
            score += min(3.5, 1.2 * len(skill.capabilities_all))
            reasons.append(f"skill_caps_all:{len(skill.capabilities_all)}")

        if skill.capabilities_any:
            matched_caps = [cap for cap in skill.capabilities_any if cap in request_capabilities]
            if not matched_caps:
                return None
            score += min(4.0, 1.3 * len(matched_caps))
            reasons.append(f"skill_caps_any:{len(matched_caps)}")

        semantic_score = skill.semantic_score(request_tokens)
        if semantic_score < skill.min_semantic_score:
            return None
        if semantic_score > 0:
            score += semantic_score * skill.semantic_weight
            reasons.append(f"skill_sem:{semantic_score:.2f}")

        return score, reasons

    def _match_pack(
        self,
        pack: TemplatePack,
        user_request: str,
        intent: UserIntent,
        world_state: WorldState,
        request_tokens: list[str],
        request_capabilities: set[str],
    ) -> Optional[tuple[dict, float, list[str]]]:
        match_cfg = pack.match or {}
        action = intent.action.value
        target = intent.target_type.value
        text = user_request.lower()
        app_text = (world_state.active_app or "").lower()
        browser_domain = self._extract_domain(world_state.browser_url or "")

        score = float(pack.priority)
        reasons: list[str] = []

        skill_match = self._match_skill(
            pack.skill,
            text=text,
            request_tokens=request_tokens,
            request_capabilities=request_capabilities,
        )
        if skill_match is None:
            return None
        skill_score, skill_reasons = skill_match
        score += skill_score
        reasons.extend(skill_reasons)

        min_conf = match_cfg.get("min_intent_confidence")
        if min_conf is not None:
            try:
                min_conf_val = float(min_conf)
            except (TypeError, ValueError):
                min_conf_val = 0.0
            if intent.confidence < min_conf_val:
                return None
            score += 2.0
            reasons.append("intent_confidence")

        intent_actions = self._norm_list(match_cfg.get("intent_actions"))
        if intent_actions and action not in intent_actions:
            return None
        if intent_actions:
            score += 8.0
            reasons.append("intent_action")

        target_types = self._norm_list(match_cfg.get("target_types"))
        if target_types and target not in target_types:
            return None
        if target_types:
            score += 4.0
            reasons.append("target_type")

        keywords_all = self._norm_list(match_cfg.get("keywords_all") or match_cfg.get("keywords"))
        if keywords_all and not all(k in text for k in keywords_all):
            return None
        if keywords_all:
            score += min(6.0, 1.3 * len(keywords_all))
            reasons.append(f"keywords_all:{len(keywords_all)}")

        any_keywords = self._norm_list(match_cfg.get("keywords_any") or match_cfg.get("any_keywords"))
        matched_any_keywords = [k for k in any_keywords if k in text]
        if any_keywords and not matched_any_keywords:
            return None
        if matched_any_keywords:
            score += min(5.0, 0.8 * len(matched_any_keywords))
            reasons.append(f"keywords_any:{len(matched_any_keywords)}")

        excluded_keywords = self._norm_list(match_cfg.get("keywords_none"))
        if excluded_keywords and any(k in text for k in excluded_keywords):
            return None

        phrases_any = self._norm_list(match_cfg.get("phrases_any"))
        matched_phrases = [p for p in phrases_any if p in text]
        if phrases_any and not matched_phrases:
            return None
        if matched_phrases:
            score += min(3.0, 0.9 * len(matched_phrases))
            reasons.append(f"phrases_any:{len(matched_phrases)}")

        captures: dict[str, str] = {}
        regex = match_cfg.get("regex")
        if regex:
            try:
                m = re.search(regex, user_request, re.IGNORECASE | re.DOTALL)
            except re.error as e:
                print(f"[TemplateRegistry] Pack '{pack.name}' regex error: {e}")
                return None
            if not m:
                return None
            captures.update({k: (v or "").strip() for k, v in m.groupdict().items()})
            score += 5.0
            reasons.append("regex")

        regex_any = self._norm_list(match_cfg.get("regex_any"), lower=False)
        if regex_any:
            found = False
            for pat in regex_any:
                try:
                    m = re.search(pat, user_request, re.IGNORECASE | re.DOTALL)
                except re.error:
                    continue
                if m:
                    found = True
                    captures.update({k: (v or "").strip() for k, v in m.groupdict().items()})
                    score += 3.0
                    reasons.append("regex_any")
                    break
            if not found:
                return None

        active_apps_any = self._norm_list(match_cfg.get("active_apps_any"))
        if active_apps_any:
            if not app_text:
                return None
            if not any(term in app_text for term in active_apps_any):
                return None
            score += 2.5
            reasons.append("active_app")

        active_apps_none = self._norm_list(match_cfg.get("active_apps_none"))
        if active_apps_none and any(term in app_text for term in active_apps_none):
            return None

        browser_domains_any = self._norm_list(match_cfg.get("browser_domains_any"))
        if browser_domains_any:
            if not browser_domain:
                return None
            if not any(term in browser_domain for term in browser_domains_any):
                return None
            score += 2.5
            reasons.append("browser_domain")

        browser_domains_none = self._norm_list(match_cfg.get("browser_domains_none"))
        if browser_domains_none and browser_domain and any(term in browser_domain for term in browser_domains_none):
            return None

        require_browser = bool(match_cfg.get("require_browser", False))
        if require_browser and not browser_domain:
            return None

        required_caps_all = self._norm_list(match_cfg.get("required_capabilities_all"))
        if required_caps_all and not all(cap in request_capabilities for cap in required_caps_all):
            return None
        if required_caps_all:
            score += min(2.5, 0.8 * len(required_caps_all))
            reasons.append(f"caps_all:{len(required_caps_all)}")

        required_caps_any = self._norm_list(match_cfg.get("required_capabilities_any"))
        if required_caps_any:
            matched_caps_any = [cap for cap in required_caps_any if cap in request_capabilities]
            if not matched_caps_any:
                return None
            score += min(2.5, 0.8 * len(matched_caps_any))
            reasons.append(f"caps_any:{len(matched_caps_any)}")

        pack_tags = self._norm_list(pack.tags)
        if pack_tags:
            matched_tags = [tag for tag in pack_tags if tag in request_capabilities]
            if matched_tags:
                score += min(2.0, 0.5 * len(matched_tags))
                reasons.append(f"tag_overlap:{len(matched_tags)}")

        min_total_score = match_cfg.get("min_total_score")
        if min_total_score is not None:
            try:
                min_total_score_val = float(min_total_score)
            except (TypeError, ValueError):
                min_total_score_val = 0.0
            if score < min_total_score_val:
                return None

        return captures, score, reasons
