from __future__ import annotations

from typing import Optional

from .models import ExperimentConfigurationError
from .shared_provider import LLMProvider, get_gemini_provider_class
from .utils import read_env_value


def load_gemini_provider(
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> LLMProvider:
    resolved_key = (api_key or read_env_value("GEMINI_API_KEY")).strip()
    if not resolved_key:
        raise ExperimentConfigurationError("GEMINI_API_KEY is not set.")

    resolved_model = (model or read_env_value("GEMINI_EXPERIMENT_MODEL", "gemini-3-flash-preview")).strip()
    GeminiProvider = get_gemini_provider_class()
    return GeminiProvider(api_key=resolved_key, model=resolved_model)
