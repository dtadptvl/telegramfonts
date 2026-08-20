"""Tests for scratch manager, sibling-prefix path traversal protections, and cleanup."""
from pathlib import Path
import pytest

from scratch import ScratchManager, is_path_contained_within


def test_is_path_contained_within(tmp_path: Path):
    root = tmp_path / "scratch"
    root.mkdir()

    valid_child = root / "job_1_token"
    valid_child.mkdir()
    assert is_path_contained_within(valid_child, root) is True

    # Sibling prefix escape: e.g. /scratch_evil (BLOCK E)
    sibling_evil = tmp_path / "scratch_evil"
    sibling_evil.mkdir()
    assert is_path_contained_within(sibling_evil, root) is False


def test_scratch_dir_creation_and_traversal_guard(tmp_path: Path):
    mgr = ScratchManager(tmp_path / "scratch")

    # Valid job dir
    job_dir = mgr.get_job_dir("job_100", "tok_abc")
    assert job_dir.exists()
    assert is_path_contained_within(job_dir, mgr.root) is True

    # Path traversal attempt in get_job_dir
    with pytest.raises(ValueError, match="EMPTY_JOB_OR_TOKEN_IDENTIFIER"):
        mgr.get_job_dir("../../etc", "../..")

    # Path traversal in resolve_safe_path
    with pytest.raises(ValueError, match="Path traversal"):
        mgr.resolve_safe_path(job_dir, "../../../secret.txt")


def test_scratch_cleanup_cannot_delete_sibling_path(tmp_path: Path):
    root = tmp_path / "scratch"
    mgr = ScratchManager(root)

    # Create sibling dir
    sibling = tmp_path / "scratch_sibling"
    sibling.mkdir()
    (sibling / "important.txt").write_text("data")

    # Attempt to cleanup sibling path
    mgr.cleanup_job_dir(sibling)
    # Sibling must still exist (BLOCK E)
    assert sibling.exists()
    assert (sibling / "important.txt").exists()

    # Valid child cleanup
    job_dir = mgr.get_job_dir("job_1", "tok_1")
    assert job_dir.exists()
    mgr.cleanup_job_dir(job_dir)
    assert not job_dir.exists()
