"""Runtime secret boundary for the Vietnamese AI cascade (R3 / ADR-0003).

Reads ONLY the exact key names ``wokushop_api_key`` and ``openrouter_api_key``
from the runtime ``dev.vars`` file (on this workstation: E:\\cv\\telefont\\
dev.vars; resolved portably, never hardcoded). Values are parsed locally,
kept in memory, passed to the cascade clients in memory, and NEVER logged,
echoed, committed, hashed, reported, or placed in artifacts.

Redaction guards: every loaded value is registered as a redaction target and
a logging filter strips it from all log records; ``redact_text`` sanitizes
any outbound string. The canary test proves no secret substring can appear
in logs/reports.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger("telegramfonts.agent.ai_secret_loader")

# Exact key names only (ADR-0003). No aliases, no case folding, no patterns.
ALLOWED_SECRET_KEYS: tuple[str, ...] = ("wokushop_api_key", "openrouter_api_key")

_REDACTED = "[REDACTED]"
_redaction_lock = threading.Lock()
_redaction_targets: list[str] = []
_filter_installed = False


class SecretRedactionFilter(logging.Filter):
    """Strips any registered secret value from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        changed = False
        with _redaction_lock:
            targets = list(_redaction_targets)
        for secret in targets:
            if secret and secret in msg:
                msg = msg.replace(secret, _REDACTED)
                changed = True
        if changed:
            record.msg = msg
            record.args = ()
        return True


def _install_redaction_filter_once() -> None:
    global _filter_installed
    with _redaction_lock:
        if _filter_installed:
            return
        root = logging.getLogger()
        redaction = SecretRedactionFilter()
        # Logger-level filters cover direct emissions; handler-level filters
        # cover propagated records at every sink (defense in depth).
        if not any(isinstance(f, SecretRedactionFilter) for f in root.filters):
            root.addFilter(redaction)
        for handler in list(root.handlers):
            if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
                handler.addFilter(redaction)
        agent_logger = logging.getLogger("telegramfonts")
        if not any(isinstance(f, SecretRedactionFilter) for f in agent_logger.filters):
            agent_logger.addFilter(redaction)
        for handler in list(agent_logger.handlers):
            if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
                handler.addFilter(redaction)
        _filter_installed = True


def register_redaction_secret(value: str) -> None:
    """Register an in-memory secret value for log redaction (value never
    leaves process memory through this module)."""
    if not value:
        return
    _install_redaction_filter_once()
    with _redaction_lock:
        if value not in _redaction_targets:
            _redaction_targets.append(value)


def redact_text(text: str) -> str:
    """Sanitize any outbound string against all registered secret values."""
    out = str(text)
    with _redaction_lock:
        targets = list(_redaction_targets)
    for secret in targets:
        if secret and secret in out:
            out = out.replace(secret, _REDACTED)
    return out


def _worktree_main_root(checkout_root: Path) -> Path | None:
    """Resolve the main checkout root of a linked git worktree via its .git
    file (gitdir pointer). Returns None for ordinary checkouts."""
    dot_git = checkout_root / ".git"
    try:
        if not dot_git.is_file():
            return None
        content = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (checkout_root / gitdir).resolve()
    parts = gitdir.parts
    if ".git" not in parts:
        return None
    idx = parts.index(".git")
    if idx == 0:
        return None
    return Path(*parts[:idx])


def _repo_root_candidates() -> list[Path]:
    """Portable dev.vars candidates: this checkout root first, then the main
    checkout root of a linked git worktree (where the non-versioned dev.vars
    lives), then bounded ancestors. No absolute path is hardcoded."""
    checkout_root = Path(__file__).resolve().parents[3]
    candidates: list[Path] = [checkout_root / "dev.vars"]
    main_root = _worktree_main_root(checkout_root)
    if main_root is not None:
        candidates.append(main_root / "dev.vars")
    current = checkout_root.parent
    for _ in range(3):
        candidates.append(current / "dev.vars")
        current = current.parent
    return candidates


def default_dev_vars_path() -> Path | None:
    """Resolve the runtime dev.vars file (existence probe only; content is
    read exclusively by ``load_ai_secrets``)."""
    for candidate in _repo_root_candidates():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def load_ai_secrets(dev_vars_path: Path | str | None = None) -> dict[str, str]:
    """Load ONLY the exact ADR-0003 key names from the runtime dev.vars.

    ``dev_vars_path`` is the ONLY sanctioned consumption point: an absent
    (None) path reads NOTHING (hermetic tests/ordinary construction never
    open the real dev.vars); the production entrypoint resolves
    ``default_dev_vars_path()`` explicitly and passes it in.

    Returns an in-memory dict (possibly empty). Fails closed to {} on any
    read/parse problem. Never logs or reports key values; every loaded value
    is registered for redaction before this function returns.
    """
    if dev_vars_path is None:
        return {}
    path = Path(dev_vars_path)
    secrets: dict[str, str] = {}
    if path is None:
        return {}
    try:
        if not path.is_file():
            return {}
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _sep, value = stripped.partition("=")
        name = name.strip()
        # Exact key names only; everything else is ignored by design.
        if name not in ALLOWED_SECRET_KEYS:
            continue
        cleaned = value.strip().strip("'\"")
        if cleaned:
            secrets[name] = cleaned
    for value in secrets.values():
        register_redaction_secret(value)
    return secrets
