"""Tests for immutable final-font archive identity, integrity, and runner reuse."""
import asyncio
import hashlib
from pathlib import Path

import pytest

from compute.archive import ArchiveIdentity, FinalFontArchive
from compute.models import ArchiveSourceContext, ClaimStyle, GeneratedFontFile
from compute.source import SourceAcquirer
from config import Settings
from queue_client import QueueMessage
from runner import JobRunner, RunnerAction
from worker_client import (
    ClaimResult,
    ClaimedJob,
    CompleteResult,
    HeartbeatResult,
    UploadResult,
)


def _font_file(
    path: Path,
    content: bytes = b"validated-font",
    style_id: str = "regular",
    style_name: str = "Regular",
    filename: str = "Demo-Regular.ttf",
) -> GeneratedFontFile:
    path.write_bytes(content)
    return GeneratedFontFile(
        style_id=style_id,
        style_name=style_name,
        format="TTF",
        filename=filename,
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
        style_observation_identities=(("regular", "observations-v1"),),
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
        observation_identity=context.observation_identity_for("regular"),
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


def test_archive_context_is_per_style_and_order_independent(tmp_path: Path):
    source = SourceAcquirer(observation_store_dir=tmp_path / "observations")
    source_url = "https://www.myfonts.com/collections/demo"
    regular = ClaimStyle("regular", "Regular")
    bold = ClaimStyle("bold", "Bold")

    try:
        source.store.save_coverage("demo", "regular", [65])
        source.store.save_coverage("demo", "bold", [65, 66])

        full = source.get_archive_context(source_url, [regular, bold])
        subset = source.get_archive_context(source_url, [regular])
        reordered = source.get_archive_context(source_url, [bold, regular])

        assert full is not None
        assert subset is not None
        assert reordered is not None
        assert full.observation_identity_for("regular") == subset.observation_identity_for("regular")
        assert full.observation_identity_for("regular") == reordered.observation_identity_for("regular")
        assert full.observation_identity_for("bold") == reordered.observation_identity_for("bold")
        assert full.observation_identity_for("regular") != full.observation_identity_for("bold")
    finally:
        asyncio.run(source.close())


@pytest.mark.asyncio
async def test_runner_subset_and_reordered_styles_hit_two_style_archive(
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch,
):
    archive = FinalFontArchive(tmp_path / "external", test_settings.SCRATCH_DIR / "archive.sqlite3")
    source_identity = "https://www.myfonts.com/collections/demo"
    style_observations = {"regular": "observations-regular", "bold": "observations-bold"}
    style_names = {"regular": "Regular", "bold": "Bold"}

    # Seed the result of the first validated two-style run.
    for style_id, content in (("regular", b"regular-font"), ("bold", b"bold-font")):
        source_file = _font_file(
            tmp_path / f"{style_id}.ttf",
            content=content,
            style_id=style_id,
            style_name=style_names[style_id],
            filename=f"Demo-{style_names[style_id]}.ttf",
        )
        archive.put(
            ArchiveIdentity(
                source_identity=source_identity,
                family_name="Demo",
                style_id=style_id,
                style_name=style_names[style_id],
                mode="ORIGINAL",
                format="TTF",
                observation_identity=style_observations[style_id],
                config_version="config-v1",
            ),
            source_file,
        )

    class QueueStub:
        def __init__(self):
            self.acks: list[str] = []

        async def acknowledge_messages(self, lease_ids):
            self.acks.extend(lease_ids)

        async def retry_messages(self, _retries):
            raise AssertionError("archive hit must not retry the queue message")

    class TrackingSource:
        store_dir = tmp_path / "source"

        def __init__(self):
            self.context_requests: list[list[str]] = []
            self.acquire_calls = 0

        def get_archive_context(self, _source_url, styles):
            self.context_requests.append([style.id for style in styles])
            return ArchiveSourceContext(
                source_identity=source_identity,
                style_observation_identities=tuple(
                    (style.id, style_observations[style.id]) for style in styles
                ),
                config_version="config-v1",
            )

        async def acquire_source(self, **_kwargs):
            self.acquire_calls += 1
            raise AssertionError("archive hit must bypass source acquisition and MAX")

    class FailingBuilder:
        def build_font(self, *_args, **_kwargs):
            raise AssertionError("archive hit must bypass font building")

    class WorkerStub:
        def __init__(self, job):
            self.job = job
            self.upload_calls = 0
            self.complete_calls = 0

        async def claim(self, _job_id):
            return ClaimResult(status="CLAIMED", queue_action="claimed", job=self.job)

        async def heartbeat(self, *_args):
            return HeartbeatResult(success=True, fenced=False, lease_expires_at=self.job.lease_expires_at)

        async def upload_artifact(self, job_id, lease_token, zip_path, sha256_hex):
            self.upload_calls += 1
            return UploadResult(
                success=True,
                fenced=False,
                artifact_key=f"archive/{job_id}.zip",
                sha256=sha256_hex,
                size=zip_path.stat().st_size,
            )

        async def complete(self, *_args, **_kwargs):
            self.complete_calls += 1
            return CompleteResult(success=True, status="COMPLETED", queue_action="ack")

        async def fail(self, *_args, **_kwargs):
            raise AssertionError("archive hit must not fail the job")

    def fail_validation(*_args, **_kwargs):
        raise AssertionError("archive hit must bypass validation")

    monkeypatch.setattr("runner.validate_font_file", fail_validation)
    source = TrackingSource()

    async def run_cached_job(job_id: str, styles: list[ClaimStyle]):
        job = ClaimedJob(
            job_id=job_id,
            order_id=job_id,
            lease_token="12345678-1234-1234-1234-123456789abc",
            lease_expires_at=9999999999999,
            source_url=source_identity,
            family_name="Demo",
            foundry=None,
            styles=styles,
            formats=["TTF"],
        )
        queue = QueueStub()
        worker = WorkerStub(job)
        runner = JobRunner(
            test_settings,
            queue_client=queue,
            worker_client=worker,
            source_acquirer=source,
            font_builder=FailingBuilder(),
            archive=archive,
        )
        result = await runner.process_message(
            QueueMessage(
                id=f"message-{job_id}",
                lease_id=f"lease-{job_id}",
                body_raw=f'{{"job_id":"{job_id}"}}',
                attempts=1,
                job_id=job_id,
            )
        )
        assert result.action == RunnerAction.ACKED
        assert queue.acks == [f"lease-{job_id}"]
        assert worker.upload_calls == 1
        assert worker.complete_calls == 1

    await run_cached_job("job_subset", [ClaimStyle("regular", "Regular")])
    await run_cached_job("job_reordered", [ClaimStyle("bold", "Bold"), ClaimStyle("regular", "Regular")])

    assert source.acquire_calls == 0
    assert source.context_requests == [["regular"], ["bold", "regular"]]
