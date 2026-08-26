"""A23 100+ Job Deterministic Restart & Multi-Tier Reuse Soak Runner CLI.

Usage:
  python scripts/a23_soak_runner.py [--jobs 100] [--seed 42] [--output ops/soak_report.json]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Add agent/src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from soak import run_a23_soak_harness


async def main() -> int:
    parser = argparse.ArgumentParser(description="A23 100+ Job Restart & Soak Harness")
    parser.add_argument("--jobs", "-j", type=int, default=100, help="Number of deterministic jobs to run (default 100)")
    parser.add_argument("--seed", "-s", type=int, default=42, help="PRNG seed for deterministic scenarios (default 42)")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output path for JSON soak summary report")
    args = parser.parse_args()

    print(f"=== Running A23 100+ Job Soak Harness (jobs={args.jobs}, seed={args.seed}) ===")
    res = await run_a23_soak_harness(num_jobs=args.jobs, seed=args.seed)

    print(f"Executed Jobs:          {res.total_jobs}")
    print(f"Completed (ACKED):      {res.completed_jobs}")
    print(f"Failed Terminal:        {res.failed_terminal_jobs}")
    print(f"Duplicate Completions:  {res.duplicate_completions}")
    print(f"Partial Publishes:      {res.partial_publishes}")
    print(f"Orphan Scratch Dirs:    {res.orphan_scratch_dirs}")
    print(f"Soak Trace Hash:        {res.soak_trace_hash}")
    print(f"Elapsed Time:           {res.elapsed_seconds}s")
    print(f"Verdict:                {'PASS' if res.passed else 'FAILED'}")

    if args.output:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(res.to_json() + "\n", encoding="utf-8")
        print(f"Soak report saved to {out_p}")

    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
