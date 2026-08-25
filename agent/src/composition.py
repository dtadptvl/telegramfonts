"""Production composition for the Stage 9D runner.

Constructs every concrete production dependency and fails closed when an
enabled required capability is not constructible. Test doubles are never
constructed here; production assembly uses only production types.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from acquisition.adapters import build_production_acquisition_pipeline
from compute.binary_cache import AuthorizedBinaryCache
from compute.model_cache import CanonicalFontModelCache

logger = logging.getLogger("telegramfonts.agent.composition")


def build_production_components(settings: Any, scratch_root: Path) -> dict[str, Any]:
    """Return concrete production components keyed by capability.

    Raises RuntimeError (fail-closed readiness) when an enabled capability
    cannot be constructed.
    """
    scratch_root = Path(scratch_root)

    model_cache = CanonicalFontModelCache(
        scratch_root / "font_model_cache",
        scratch_root / "font_model_cache_index.sqlite3",
    )
    binary_cache = AuthorizedBinaryCache(
        scratch_root / "authorized_binary_cache",
        scratch_root / "authorized_binary_cache_index.sqlite3",
    )

    try:
        acquisition_pipeline = build_production_acquisition_pipeline(settings)
    except Exception as exc:
        raise RuntimeError(f"COMPOSITION_READINESS_FAILED: {exc}") from exc

    vietnamese_ai_provider = None
    if getattr(settings, "VIETNAMESE_AI_ENABLED", False):
        key = getattr(settings, "OPENROUTER_API_KEY", None)
        key_value = key.get_secret_value() if key is not None else ""
        if not key_value:
            raise RuntimeError("COMPOSITION_READINESS_FAILED_OPENROUTER")
        from compute.openrouter_client import OpenRouterAIClient

        vietnamese_ai_provider = OpenRouterAIClient(key_value)

    return {
        "acquisition_pipeline": acquisition_pipeline,
        "model_cache": model_cache,
        "binary_cache": binary_cache,
        "vietnamese_ai_provider": vietnamese_ai_provider,
    }
