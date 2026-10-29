from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..shared_provider import LLMProvider
from .ax_first import AXFirstAgent
from .hybrid_router import HybridRouterAgent
from .vision_first import VisionFirstAgent


def create_agent(
    agent_name: str,
    provider: LLMProvider,
    artifact_root: Optional[Path] = None,
):
    normalized = str(agent_name or "").strip().lower()
    if normalized == "ax_first":
        return AXFirstAgent(provider=provider, artifact_root=artifact_root)
    if normalized == "vision_first":
        return VisionFirstAgent(provider=provider, artifact_root=artifact_root)
    if normalized == "hybrid_router":
        return HybridRouterAgent(provider=provider, artifact_root=artifact_root)
    raise ValueError(f"Unknown experiment agent '{agent_name}'")


__all__ = ["AXFirstAgent", "VisionFirstAgent", "HybridRouterAgent", "create_agent"]
