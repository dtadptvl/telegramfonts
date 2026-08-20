"""Entrypoint for A23 Private Compute Worker process."""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from config import Settings
from logging_utils import setup_logging
from queue_client import CloudflareQueueClient
from runner import A23Runner
from scratch import ScratchManager
from worker_client import WorkerJobClient

logger = setup_logging()


async def main() -> None:
    settings = Settings()
    scratch_manager = ScratchManager(settings.SCRATCH_DIR)

    # Prune stale scratch dirs on startup
    pruned = scratch_manager.prune_stale_dirs()
    if pruned > 0:
        logger.info(f"Pruned {pruned} stale scratch directories on startup")

    queue_client = CloudflareQueueClient(settings)
    worker_client = WorkerJobClient(settings)
    runner = A23Runner(
        settings=settings,
        queue_client=queue_client,
        worker_client=worker_client,
        scratch_manager=scratch_manager,
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
        f"queue={settings.CF_QUEUE_ID}, edge={settings.EDGE_BASE_URL})"
    )

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
