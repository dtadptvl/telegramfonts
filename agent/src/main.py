"""Entrypoint for A23 Private Compute Worker process."""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from composition import build_production_components
from compute.ai_secret_loader import default_dev_vars_path
from compute.archive import resolve_archive_mode
from config import Settings
from logging_utils import setup_logging
from queue_client import CloudflareQueueClient
from runner import A23Runner, touch_progress_beacon
from scratch import ScratchManager
from worker_client import WorkerJobClient

logger = setup_logging()


async def main() -> None:
    settings = Settings()
    # D21 safe archive mode (Issue #90): resolve and announce the explicit,
    # versioned archive-mode identity at startup; never silent.
    archive_mode = resolve_archive_mode(settings)
    scratch_manager = ScratchManager(settings.SCRATCH_DIR)

    # Prune stale scratch dirs on startup
    pruned = scratch_manager.prune_stale_dirs()
    if pruned > 0:
        logger.info(f"Pruned {pruned} stale scratch directories on startup")

    queue_client = CloudflareQueueClient(settings)
    worker_client = WorkerJobClient(settings)
    # Stage 9D production composition: concrete L2/L3/acquisition/AI deps;
    # readiness fails closed when an enabled capability is not constructible.
    # The non-versioned dev.vars-shaped OpenRouter key (key-only shape) is
    # consumed explicitly here and nowhere else.
    components = build_production_components(
        settings, scratch_manager.root, dev_vars_path=default_dev_vars_path()
    )
    runner = A23Runner(
        settings=settings,
        queue_client=queue_client,
        worker_client=worker_client,
        scratch_manager=scratch_manager,
        acquisition_pipeline=components["acquisition_pipeline"],
        model_cache=components["model_cache"],
        binary_cache=components["binary_cache"],
        vietnamese_ai_provider=components["vietnamese_ai_provider"],
        # R1: DEFAULT production atlas factory (real transport chain); no
        # deployment-phase remainder. Absent when the acquisition capability
        # is disabled - the runner then fails closed with
        # ATLAS_RASTER_SOURCE_UNAVAILABLE.
        atlas_pipeline_factory=components["atlas_pipeline_factory"],
    )

    stop_event = asyncio.Event()

    def handle_signal(*_: object) -> None:
        logger.info("Shutdown signal received; stopping consumer loop...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass  # Windows signal handling fallback

    logger.info(
        f"A23 Compute Worker started (worker_id={settings.A23_WORKER_ID}, "
        f"queue={settings.CF_QUEUE_ID}, edge={settings.EDGE_BASE_URL}, "
        f"archive_mode={archive_mode.identity})"
    )

    # T-FAST30-A23-FIX F5: seed the supervisor progress beacon at startup so
    # the hang watchdog has a baseline before the first loop iteration.
    touch_progress_beacon(settings.PROGRESS_BEACON_FILE, "worker_start")

    try:
        await runner.run_loop(stop_event=stop_event)
    finally:
        await runner.close()
        logger.info("A23 Compute Worker stopped successfully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
