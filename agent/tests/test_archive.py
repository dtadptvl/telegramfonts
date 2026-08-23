"""Tests for immutable final-font archive identity, integrity, and runner reuse."""
import asyncio
import hashlib
from pathlib import Path

from compute.archive import ArchiveIdentity, FinalFontArchive
from compute.models import ArchiveSourceContext, ClaimStyle, GeneratedFontFile
from config import Settings
from runner import JobRunner
from worker_client import ClaimedJob


def _font_file(path: Path, content: bytes = b"validated-font") -> GeneratedFontFile:
    path.write_bytes(content)
    return GeneratedFontFile(
        style_id="regular",
        style_name="Regular",
        format="TTF",
        filename="Demo-Regular.ttf",
        file_path=path,
        size_bytes=len(content),
        sha256_hex=hashlib.sha256(content).hexdigest(),
    )


def _identity(mode: str = "ORIGINAL", fmt: str = "TTF") -> ArchiveIdentity:
    return ArchiveIdentity(
        source_identity="https://www.myfonts.com/collections/demo",
        family_name="Demo",
        style_id="regular",
        style_name="Regular",
        mode=mode,
        format=fmt,
        observation_identity="observations-v1",
        config_version="config-v1",
    )


def test_archive_stores_atomically_and_rejects_corrupt_hits(tmp_path: Path):
    archive_root = tmp_path / "external-ext4"
    index_path = tmp_path / "internal" / "font_archive.sqlite3"
    archive = FinalFontArchive(archive_root, index_path)
    source = _font_file(tmp_path / "Demo-Regular.ttf")

    entry = archive.put(_identity(), source)
    assert entry.file_path.is_file()
    assert entry.file_path.is_relative_to(archive_root)
    assert not index_path.is_relative_to(archive_root)
    assert archive.get(_identity()) == entry

    entry.file_path.write_bytes(b"corrupt")
    assert archive.get(_identity()) is None

    repaired = archive.put(_identity(), source)
    assert repaired.file_path.is_file()
    assert repaired.file_path != entry.file_path
    assert archive.get(_identity()) == repaired


def test_archive_identity_separates_required_dimensions():
    base = _identity()
    assert base.cache_key != _identity(mode="VIETNAMESE").cache_key
    assert base.cache_key != _identity(fmt="OTF").cache_key
    assert base.cache_key != ArchiveIdentity(
        source_identity=base.source_identity,
        family_name=base.family_name,
        style_id=base.style_id,
        style_name=base.style_name,
        mode=base.mode,
        format=base.format,
        observation_identity="observations-v2",
        config_version=base.config_version,
    ).cache_key
    assert base.cache_key != ArchiveIdentity(
        source_identity=base.source_identity,
        family_name=base.family_name,
        style_id=base.style_id,
        style_name=base.style_name,
        mode=base.mode,
        format=base.format,
        observation_identity=base.observation_identity,
        config_version="config-v2",
    ).cache_key


def test_archive_index_defaults_to_internal_scratch_storage(test_settings: Settings, tmp_path: Path):
    settings = test_settings.model_copy(update={"FONT_ARCHIVE_ROOT": tmp_path / "external"})
    archive = FinalFontArchive.from_settings(settings)
    assert archive is not None
    assert archive.index_path == (settings.SCRATCH_DIR / "font_archive_index.sqlite3").resolve()
    assert not archive.index_path.is_relative_to(settings.FONT_ARCHIVE_ROOT.resolve())


def test_runner_packages_verified_archive_hit_without_builder(test_settings: Settings, tmp_path: Path):
    archive = FinalFontArchive(tmp_path / "external", test_settings.SCRATCH_DIR / "archive.sqlite3")
    source_file = _font_file(tmp_path / "source.ttf")
    context = ArchiveSourceContext(
        source_identity="https://www.myfonts.com/collections/demo",
        observation_identity="observations-v1",
        config_version="config-v1",
    )
    job = ClaimedJob(
        job_id="job_archive_hit",
        order_id="order_archive_hit",
        lease_token="12345678-1234-1234-1234-123456789abc",
        lease_expires_at=9999999999999,
        source_url="https://www.myfonts.com/collections/demo",
        family_name="Demo",
        foundry=None,
        styles=[ClaimStyle("regular", "Regular")],
        formats=["TTF"],
    )
    identity = ArchiveIdentity(
        source_identity=context.source_identity,
        family_name="Demo",
        style_id="regular",
        style_name="Regular",
        mode=job.mode,
        format="TTF",
        observation_identity=context.observation_identity,
        config_version=context.config_version,
    )
    archive.put(identity, source_file)

    class FailingBuilder:
        def build_font(self, *args, **kwargs):
            raise AssertionError("archive hit must bypass font builder")

    runner = JobRunner(
        test_settings,
        queue_client=object(),
        worker_client=object(),
        font_builder=FailingBuilder(),
        archive=archive,
    )
    hit = runner._get_archive_hit(job, "Demo", context)
    assert hit is not None
    manifest = runner._sync_build_validate_and_package(
        source_payload=None,
        job=job,
        job_dir=tmp_path / "job",
        fenced_event=asyncio.Event(),
        expiry_holder=[9999999999999],
        archive_context=context,
        cached_files=hit,
    )
    assert manifest.files[0].file_path == archive.get(identity).file_path
