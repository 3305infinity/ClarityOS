"""
Moonwalk — Browser Snapshot Store
=================================
In-memory state for browser sessions, snapshots, stable refs,
and a tab ledger that tracks every known open tab.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .models import PageSnapshot, ElementRef


# ── Tab tracking ──

@dataclass
class TabInfo:
    """Lightweight record of a known browser tab."""
    tab_id: str
    url: str
    title: str = ""
    domain: str = ""
    last_seen: float = 0.0
    session_id: str = ""

    def matches_url(self, target_url: str) -> bool:
        """Check if this tab already has the target URL open (exact or same-page)."""
        if not target_url or not self.url:
            return False
        # Exact match (ignore trailing slash)
        a = self.url.rstrip("/").lower()
        b = target_url.rstrip("/").lower()
        if a == b:
            return True
        # Same page with different fragment
        pa, pb = urlparse(a), urlparse(b)
        if pa.scheme == pb.scheme and pa.netloc == pb.netloc and pa.path == pb.path and pa.query == pb.query:
            return True
        return False

    def matches_domain(self, target_url: str) -> bool:
        """Check if this tab is on the same domain as the target URL."""
        if not target_url or not self.domain:
            return False
        try:
            target_domain = urlparse(target_url.lower()).netloc.replace("www.", "")
            return target_domain and target_domain == self.domain
        except Exception:
            return False


class BrowserStore:
    def __init__(self):
        self._snapshots: Dict[str, PageSnapshot] = {}
        self._refs: Dict[str, Dict[str, ElementRef]] = {}
        self._current_session_id: Optional[str] = None
        # ── Tab ledger: tab_id → TabInfo ──
        self._tabs: Dict[str, TabInfo] = {}

    # ── Snapshot management ──

    def upsert_snapshot(self, snapshot: PageSnapshot) -> None:
        self._snapshots[snapshot.session_id] = snapshot
        self._refs[snapshot.session_id] = {el.ref_id: el for el in snapshot.elements}
        self._current_session_id = snapshot.session_id
        # Register / update the tab in the ledger
        self._register_tab(snapshot)

    def _register_tab(self, snapshot: PageSnapshot) -> None:
        """Update the tab ledger whenever a snapshot arrives."""
        tab_id = snapshot.tab_id or snapshot.session_id
        domain = ""
        try:
            domain = urlparse(snapshot.url.lower()).netloc.replace("www.", "")
        except Exception:
            pass
        self._tabs[tab_id] = TabInfo(
            tab_id=tab_id,
            url=snapshot.url,
            title=snapshot.title,
            domain=domain,
            last_seen=time.time(),
            session_id=snapshot.session_id,
        )

    def get_snapshot(self, session_id: Optional[str] = None) -> Optional[PageSnapshot]:
        sid = session_id or self._current_session_id
        if not sid:
            return None
        return self._snapshots.get(sid)

    def get_element(self, ref_id: str, session_id: Optional[str] = None) -> Optional[ElementRef]:
        sid = session_id or self._current_session_id
        if not sid:
            return None
        return self._refs.get(sid, {}).get(ref_id)

    def list_elements(self, session_id: Optional[str] = None) -> List[ElementRef]:
        snapshot = self.get_snapshot(session_id)
        return snapshot.elements[:] if snapshot else []

    def has_snapshot(self, session_id: Optional[str] = None) -> bool:
        return self.get_snapshot(session_id) is not None

    def current_generation(self, session_id: Optional[str] = None) -> int:
        snapshot = self.get_snapshot(session_id)
        return snapshot.generation if snapshot else 0

    def invalidate_session(self, session_id: str) -> None:
        self._snapshots.pop(session_id, None)
        self._refs.pop(session_id, None)
        if self._current_session_id == session_id:
            self._current_session_id = None

    # ── Tab ledger ──

    def get_tabs(self) -> List[TabInfo]:
        """Return all known open tabs, most recently seen first."""
        tabs = sorted(self._tabs.values(), key=lambda t: t.last_seen, reverse=True)
        return tabs

    def find_tab_by_url(self, url: str) -> Optional[TabInfo]:
        """Find a tab with an exact or same-page URL match."""
        for tab in self._tabs.values():
            if tab.matches_url(url):
                return tab
        return None

    def find_tab_by_domain(self, url: str) -> Optional[TabInfo]:
        """Find the most recently seen tab on the same domain."""
        matches = [t for t in self._tabs.values() if t.matches_domain(url)]
        if not matches:
            return None
        return max(matches, key=lambda t: t.last_seen)

    def register_external_tabs(self, tabs: List[Dict]) -> None:
        """Bulk-register tabs from an external source (e.g., AppleScript tab query)."""
        for t in tabs:
            tab_id = str(t.get("tab_id", t.get("index", "")))
            url = t.get("url", "")
            title = t.get("title", "")
            domain = ""
            try:
                domain = urlparse(url.lower()).netloc.replace("www.", "")
            except Exception:
                pass
            if tab_id and url:
                self._tabs[tab_id] = TabInfo(
                    tab_id=tab_id, url=url, title=title,
                    domain=domain, last_seen=time.time(),
                )

    def remove_tab(self, tab_id: str) -> None:
        self._tabs.pop(tab_id, None)

    def reset(self) -> None:
        self._snapshots.clear()
        self._refs.clear()
        self._tabs.clear()
        self._current_session_id = None


browser_store = BrowserStore()
