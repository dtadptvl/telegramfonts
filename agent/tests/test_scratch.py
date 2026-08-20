"""Tests for scratch manager and path traversal protections."""
from pathlib import Path
import pytest

from scratch import ScratchManager


def test_scratch_dir_creation_and_traversal_guard(tmp_path: Path):
    mgr = ScratchManager(tmp_path)

    # Valid job dir
    job_dir = mgr.get_job_dir("job_100", "tok_abc")
    assert job_dir.exists()
    assert str(job_dir).startswith(str(tmp_path))

    # Path traversal attempt in get_job_dir
    bad_dir = mgr.get_job_dir("../../etc", "passwd")
    assert str(bad_dir).startswith(str(tmp_path))

    # Path traversal in resolve_safe_path
    with pytest.raises(ValueError, match="Path traversal"):
        mgr.resolve_safe_path(job_dir, "../../../secret.txt")


def test_scratch_cleanup_and_pruning(tmp_path: Path):
    mgr = ScratchManager(tmp_path)
    job_dir = mgr.get_job_dir("job_1", "tok_1")
    (job_dir / "test.txt").write_text("data")
    assert job_dir.exists()

    mgr.cleanup_job_dir(job_dir)
    assert not job_dir.exists()
