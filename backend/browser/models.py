"""
Moonwalk — Browser Models
=========================
Typed contracts for stable browser element mapping.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time


@dataclass
class ElementFingerprint:
    role: str = ""
    text: str = ""
    aria_label: str = ""
    name: str = ""
    placeholder: str = ""
    href: str = ""
    ancestor_labels: List[str] = field(default_factory=list)
    frame_path: str = "main"
    dom_path: str = ""
    sibling_index: int = 0
    stable_attributes: Dict[str, str] = field(default_factory=dict)


@dataclass
class ElementRef:
    ref_id: str
    generation: int
    agent_id: int = 0
    role: str = ""
    tag: str = ""
    text: str = ""
    aria_label: str = ""
    name: str = ""
    placeholder: str = ""
    href: str = ""
    value: str = ""
    context_text: str = ""
    frame_path: str = "main"
    dom_path: str = ""
    bounds: Dict[str, int] = field(default_factory=dict)
    visible: bool = True
    enabled: bool = True
    checked: bool = False
    selected: bool = False
    in_viewport: bool = True
    action_types: List[str] = field(default_factory=list)
    fingerprint: ElementFingerprint = field(default_factory=ElementFingerprint)

    def primary_label(self) -> str:
        return self.text or self.aria_label or self.name or self.placeholder or self.href or self.tag

    def supports(self, action: str) -> bool:
        return action in self.action_types if self.action_types else True


@dataclass
class ViewportMeta:
    """Viewport dimensions and scroll offsets reported by the extension."""
    width: int = 0
    height: int = 0
    scroll_x: int = 0
    scroll_y: int = 0
    scroll_height: int = 0
    page_height: int = 0


@dataclass
class PageSnapshot:
    session_id: str
    tab_id: str
    url: str
    title: str = ""
    generation: int = 1
    timestamp: float = field(default_factory=time.time)
    frame_id: str = "main"
    elements: List[ElementRef] = field(default_factory=list)
    opaque_regions: List[Dict[str, str]] = field(default_factory=list)
    viewport: ViewportMeta = field(default_factory=ViewportMeta)


@dataclass
class ActionRequest:
    action: str
    ref_id: str
    action_id: str = ""
    session_id: str = ""
    text: str = ""
    option: str = ""
    clear_first: bool = False
    timeout: float = 5.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    ok: bool
    message: str
    action: str
    ref_id: str = ""
    action_id: str = ""
    session_id: str = ""
    pre_generation: int = 0
    post_generation: int = 0
    details: Dict[str, Any] = field(default_factory=dict)
    error: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationReport:
    success: bool
    confidence: float
    message: str
    checks_passed: List[str] = field(default_factory=list)
    pre_generation: int = 0
    post_generation: int = 0
    needs_replan: bool = False


@dataclass
class DomChangeEvent:
    """Pushed by the MutationObserver after an action triggers DOM mutations."""
    action_id: str
    ref_id: str = ""
    action_type: str = ""
    change_types: List[str] = field(default_factory=list)
    timestamp: float = 0.0
    session_id: str = ""
    tab_id: str = ""
