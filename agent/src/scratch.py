"""Scratch workspace management and path traversal protections."""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger("telegramfonts.agent.scratch")


def is_path_contained_within(child: Path, parent: Path) -> bool:
    """Verify that child path is strictly a subpath of parent (not a sibling prefix)."""
    try:
        child_resolved = child.resolve()
        parent_resolved = parent.resolve()
        child_resolved.relative_to(parent_resolved)
        return True
    except ValueError:
        return False


class ScratchManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def get_job_dir(self, job_id: str, lease_token: str) -> Path:
        clean_job = re.sub(r"[^a-zA-Z0-9_-]", "", job_id.strip())
        clean_token = re.sub(r"[^a-zA-Z0-9_-]", "", lease_token.strip())

        if not clean_job or not clean_token:
            raise ValueError("EMPTY_JOB_OR_TOKEN_IDENTIFIER")

        job_dir = (self.root / f"{clean_job}_{clean_token}").resolve()

        if not is_path_contained_within(job_dir, self.root) or job_dir == self.root:
            raise ValueError(f"Path traversal detected for job dir: {job_dir}")

        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir

    def get_durable_job_cache_dir(self, job_id: str, namespace: str) -> Path:
        """Durable per-job cache directory that survives lease re-claims.

        Unlike ``get_job_dir`` (which binds the lease token and is deleted on
        cleanup), this path is keyed ONLY by the sanitized job id under a
        namespaced cache root (analogous to ``font_model_cache``). A re-claimed
        attempt of the same job resolves the same directory, so persisted
        identity-bound state (checkpoints) resumes instead of restarting.
        Distinct jobs never share a directory; traversal fails closed.
        """
        clean_job = re.sub(r"[^a-zA-Z0-9_-]", "", job_id.strip())
        clean_ns = re.sub(r"[^a-zA-Z0-9_-]", "", namespace.strip())

        if not clean_job or not clean_ns:
            raise ValueError("EMPTY_JOB_OR_NAMESPACE_IDENTIFIER")

        target = (self.root / clean_ns / clean_job).resolve()

        if not is_path_contained_within(target, self.root) or target == self.root:
            raise ValueError(f"Path traversal detected for durable cache dir: {target}")

        target.mkdir(parents=True, exist_ok=True)
        return target

    def resolve_safe_path(self, base_dir: Path, relative_name: str) -> Path:
        base_resolved = base_dir.resolve()
        target = (base_resolved / relative_name).resolve()
        if not is_path_contained_within(target, base_resolved):
            raise ValueError(f"Path traversal attempt: {relative_name}")
        return target

    def cleanup_job_dir(self, job_dir: Path) -> None:
        try:
            resolved = job_dir.resolve()
            if is_path_contained_within(resolved, self.root) and resolved != self.root and resolved.exists():
                shutil.rmtree(resolved, ignore_errors=True)
                logger.debug(f"Cleaned up scratch dir: {resolved}")
        except Exception as exc:
            logger.warning(f"Failed to cleanup scratch dir {job_dir}: {exc}")

    def prune_stale_dirs(self, max_age_seconds: int = 86400) -> int:
        """Prune scratch directories older than max_age_seconds on startup."""
        pruned = 0
        now = time.time()
        try:
            for entry in self.root.iterdir():
                if entry.is_dir() and is_path_contained_within(entry, self.root) and entry.resolve() != self.root:
                    try:
                        mtime = entry.stat().st_mtime
                        if now - mtime > max_age_seconds:
                            shutil.rmtree(entry, ignore_errors=True)
                            pruned += 1
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning(f"Error while pruning scratch dirs: {exc}")
        return pruned
