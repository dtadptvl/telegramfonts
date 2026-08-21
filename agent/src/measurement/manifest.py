"""Reproducibility manifest capturing full runtime environment, library versions, and config hashes."""
from __future__ import annotations

import datetime
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any

from measurement.models import ObservationConfig


@dataclass(frozen=True)
class ReproducibilityManifest:
    """Immutable manifest documenting environment, toolchain, and configuration for reproducible measurement."""

    git_commit: str
    git_is_dirty: bool
    os_name: str
    os_release: str
    architecture: str
    python_version: str
    chromium_version: str
    playwright_version: str | None
    freetype_version: str | None
    harfbuzz_version: str | None
    fonttools_version: str
    config_hash: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        """Convert manifest to JSON-serializable dictionary."""
        return asdict(self)


def get_git_info() -> tuple[str, bool]:
    """Resolve current git commit SHA and dirty working tree status."""
    sha = "unknown"
    is_dirty = False
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0:
            sha = res.stdout.strip()

        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if status_res.returncode == 0:
            is_dirty = len(status_res.stdout.strip()) > 0
    except Exception:
        pass

    if sha == "unknown":
        sha = os.environ.get("GIT_SHA", "unknown")

    return sha, is_dirty


def get_fonttools_version() -> str:
    """Retrieve fontTools library version."""
    try:
        import fontTools

        return getattr(fontTools, "__version__", "unknown")
    except Exception:
        return "unknown"


def get_freetype_version() -> str | None:
    """Retrieve FreeType library version if available."""
    try:
        import freetype

        return getattr(freetype, "__version__", None) or str(freetype.version())
    except Exception:
        return None


def get_harfbuzz_version() -> str | None:
    """Retrieve HarfBuzz library version if available."""
    try:
        import uharfbuzz

        return getattr(uharfbuzz, "__version__", None) or str(uharfbuzz.version())
    except Exception:
        return None


def get_playwright_version() -> str | None:
    """Retrieve Playwright library version if installed."""
    try:
        import playwright

        return getattr(playwright, "__version__", None)
    except Exception:
        return None


def create_reproducibility_manifest(
    config: ObservationConfig,
    chromium_version: str = "unknown",
) -> ReproducibilityManifest:
    """Generate comprehensive reproducibility manifest for an observation run."""
    git_sha, git_is_dirty = get_git_info()
    ft_version = get_fonttools_version()
    freetype_v = get_freetype_version()
    hb_v = get_harfbuzz_version()
    pw_v = get_playwright_version()

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    return ReproducibilityManifest(
        git_commit=git_sha,
        git_is_dirty=git_is_dirty,
        os_name=platform.system(),
        os_release=platform.release(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        chromium_version=chromium_version,
        playwright_version=pw_v,
        freetype_version=freetype_v,
        harfbuzz_version=hb_v,
        fonttools_version=ft_version,
        config_hash=config.compute_hash(),
        timestamp=now_iso,
    )
