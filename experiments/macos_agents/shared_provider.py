from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from .utils import repo_root


def _provider_dir() -> Path:
    return repo_root() / "backend" / "providers"


def _load_module(module_name: str, file_path: Path):
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_legacy_provider_package() -> types.ModuleType:
    package = sys.modules.get("providers")
    if package is None:
        package = types.ModuleType("providers")
        package.__path__ = [str(_provider_dir())]
        sys.modules["providers"] = package
    return package


def _load_base_module():
    package = _ensure_legacy_provider_package()
    module = _load_module("providers.base", _provider_dir() / "base.py")
    setattr(package, "base", module)
    return module


_BASE = _load_base_module()
LLMProvider = _BASE.LLMProvider
LLMResponse = _BASE.LLMResponse
ToolCall = _BASE.ToolCall


def get_gemini_provider_class():
    package = _ensure_legacy_provider_package()
    module = _load_module("providers.gemini", _provider_dir() / "gemini.py")
    setattr(package, "gemini", module)
    return module.GeminiProvider
