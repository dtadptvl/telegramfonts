"""Production composition for the Stage 9D runner.

Constructs every concrete production dependency and fails closed when an
enabled required capability is not constructible. Test doubles are never
constructed here; production assembly uses only production types.

Runtime secret boundary: the non-versioned dev.vars-shaped OpenRouter key
(lowercase ``openrouter_api_key``) is consumed ONLY here, through the
explicit ``load_dev_vars_secret`` loader with an explicit path supplied by
the production entrypoint. Ordinary Settings construction and tests never
open the real dev.vars file.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from acquisition.adapters import build_production_acquisition_pipeline
from compute.binary_cache import AuthorizedBinaryCache
from compute.model_cache import CanonicalFontModelCache

logger = logging.getLogger("telegramfonts.agent.composition")


def load_dev_vars_secret(dev_vars_path: Path | str, key: str) -> str:
    """Parse one secret value from a dev.vars-shaped file (``key = value``).

    Explicit, bounded and sanitized: the file is only ever read at the
    production composition boundary; values are never logged, printed, or
    embedded; a missing file or key yields "" and the caller fails closed.
    Key match is case-insensitive so the lowercase key shape loads exactly.
    """
    try:
        path = Path(dev_vars_path)
        if not path.is_file():
            return ""
        target = key.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _sep, value = stripped.partition("=")
            if name.strip().lower() == target:
                return value.strip().strip("'\"")
        return ""
    except Exception:
        return ""


def default_dev_vars_path() -> Path | None:
    """Resolve the non-versioned runtime dev.vars file for production launch.

    Portable: resolved relative to the process working directory; an absent
    file yields None. Existence is probed only — content is read exclusively
    by ``load_dev_vars_secret`` at the composition boundary.
    """
    for name in (".dev.vars", "dev.vars"):
        candidate = Path.cwd() / name
        try:
            if candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


def build_production_components(
    settings: Any,
    scratch_root: Path,
    dev_vars_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return concrete production components keyed by capability.

    Raises RuntimeError (fail-closed readiness) when an enabled capability
    cannot be constructed. ``dev_vars_path`` is the ONLY sanctioned dev.vars
    consumption point; when None, no dev.vars file is ever read.
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

    key = getattr(settings, "OPENROUTER_API_KEY", None)
    key_value = key.get_secret_value() if key is not None else ""
    if not key_value and dev_vars_path is not None:
        # Explicit runtime boundary: the non-versioned dev.vars-shaped
        # lowercase openrouter_api_key (key-only shape) is consumed safely.
        key_value = load_dev_vars_secret(dev_vars_path, "openrouter_api_key")

    vietnamese_ai_provider = None
    if key_value:
        # Key-only runtime: with a key available the fixed OpenRouter
        # provider is constructible for VIETNAMESE missing coverage.
        # ORIGINAL and complete-VI paths remain zero-call by construction
        # (the runner/extension service never invoke the provider there).
        from compute.openrouter_client import OpenRouterAIClient

        vietnamese_ai_provider = OpenRouterAIClient(key_value)
    elif getattr(settings, "VIETNAMESE_AI_ENABLED", False):
        # Explicitly enabled AI without any key source fails closed. When AI
        # is not enabled, a missing key fails closed only if AI is actually
        # required later (VI_AI_PROVIDER_UNAVAILABLE at extension time).
        raise RuntimeError("COMPOSITION_READINESS_FAILED_OPENROUTER")

    return {
        "acquisition_pipeline": acquisition_pipeline,
        "model_cache": model_cache,
        "binary_cache": binary_cache,
        "vietnamese_ai_provider": vietnamese_ai_provider,
    }
