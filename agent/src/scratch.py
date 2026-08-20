"""Scratch workspace management and path traversal protections."""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger("telegramfonts.agent.scratch")


class ScratchManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def get_job_dir(self, job_id: str, lease_token: str) -> Path:
        # Sanitize identifiers
        clean_job = "".join(c for c in job_id if c.isalnum() or c in ("-", "_"))
        clean_token = "".join(c for c in lease_token if c.isalnum() or c in ("-", "_"))

        job_dir = (self.root / f"{clean_job}_{clean_token}").resolve()

        # Path traversal guard
        if not str(job_dir).startswith(str(self.root)):
            raise ValueError(f"Path traversal detected for job dir: {job_dir}")

        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir

    def resolve_safe_path(self, base_dir: Path, relative_name: str) -> Path:
        target = (base_dir / relative_name).resolve()
        if not str(target).startswith(str(base_dir.resolve())):
            raise ValueError(f"Path traversal attempt: {relative_name}")
        return target

    def cleanup_job_dir(self, job_dir: Path) -> None:
        try:
            resolved = job_dir.resolve()
            if str(resolved).startswith(str(self.root)) and resolved != self.root and resolved.exists():
                shutil.rmtree(resolved, ignore_errors=True)
                logger.debug(f"Cleaned up scratch dir: {resolved}")
        except Exception as exc:
            logger.warning(f"Failed to cleanup scratch dir {job_dir}: {exc}")

    def prune_stale_dirs(self, max_age_seconds: int = 86400) -> int:
        """Prune scratch directories older than max_age_seconds on startup."""
        pruned = 0
        now = time.time()
        for entry in self.root.iterdir():
            if entry.is_dir():
                try:
                    mtime = entry.stat().st_mtime
                    if now - mtime > max_age_seconds:
                        shutil.rmtree(entry, ignore_errors=True)
                        pruned += 1
                except Exception:
                    pass
        return pruned
