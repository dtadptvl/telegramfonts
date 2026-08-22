"""Helper script to run on A23 for controlled E2E cutover testing."""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
from pathlib import Path
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent.parent / "agent" / "src"))

from config import Settings
from queue_client import CloudflareQueueClient
from runner import A23Runner
from scratch import ScratchManager
from worker_client import WorkerJobClient


async def seed_source_cache(url: str) -> None:
    s = Settings()
    p = s.SCRATCH_DIR / "source_cache"
    p.mkdir(parents=True, exist_ok=True)
    img = Image.new("L", (200, 200), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 20, 80, 180], fill=0)
    draw.rectangle([80, 20, 160, 60], fill=0)
    draw.rectangle([80, 80, 140, 120], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    fname = f"prev_{re.sub(r'[^a-zA-Z0-9_-]', '_', url.strip())}.bin"
    (p / fname).write_bytes(buf.getvalue())
    print(f"SOURCE_CACHE_SEEDED: {p / fname}")


async def send_queue_message(job_id: str) -> None:
    import httpx
    s = Settings()
    url = f"https://api.cloudflare.com/client/v4/accounts/{s.CF_ACCOUNT_ID}/queues/{s.CF_QUEUE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {s.CF_QUEUES_TOKEN.get_secret_value()}",
        "Content-Type": "application/json",
    }
    payload = {"body": {"job_id": job_id}, "content_type": "json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            payload_batch = {"messages": [{"body": {"job_id": job_id}}]}
            resp = await client.post(url, headers=headers, json=payload_batch)
        print(f"QUEUE_SEND_HTTP_STATUS: {resp.status_code}")
        if resp.status_code in (200, 201):
            print("QUEUE_SEND_SUCCESS")
        else:
            print(f"QUEUE_SEND_ERROR: {resp.text}")


async def run_single_job() -> None:
    s = Settings()
    sm = ScratchManager(s.SCRATCH_DIR)
    qc = CloudflareQueueClient(s)
    wc = WorkerJobClient(s)
    runner = A23Runner(s, qc, wc, sm)
    res = await runner.run_once()
    await runner.close()
    print(f"RUNNER_RESULT: {res}")


async def check_reclaim(job_id: str) -> None:
    s = Settings()
    wc = WorkerJobClient(s)
    claim = await wc.claim(job_id)
    await wc.close()
    print(f"RECLAIM_ACTION: {claim.queue_action}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True, choices=["seed_cache", "send_queue", "run_job", "check_reclaim"])
    parser.add_argument("--job-id", default="job_cutover_prod_1")
    parser.add_argument("--url", default="https://www.myfonts.com/collections/be-vietnam-pro")
    args = parser.parse_args()

    if args.action == "seed_cache":
        await seed_source_cache(args.url)
    elif args.action == "send_queue":
        await send_queue_message(args.job_id)
    elif args.action == "run_job":
        await run_single_job()
    elif args.action == "check_reclaim":
        await check_reclaim(args.job_id)


if __name__ == "__main__":
    asyncio.run(main())
