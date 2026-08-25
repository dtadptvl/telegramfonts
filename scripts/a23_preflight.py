"""A23 Compute Worker Production Readiness CLI.

Usage:
  python scripts/a23_preflight.py [--strict] [--json] [--output report.json]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add agent/src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from readiness import run_a23_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="A23 Production Readiness & Preflight Check")
    parser.add_argument("--strict", action="store_true", help="Enforce strict production checks (e.g. HTTPS)")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON output")
    parser.add_argument("--output", "-o", type=str, default=None, help="Save report JSON to path")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    report = run_a23_preflight(root_dir=repo_root, strict=args.strict)

    if args.output:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(report.to_json() + "\n", encoding="utf-8")
        print(f"Preflight report saved to {out_p}")

    if args.json:
        print(report.to_json())
    else:
        print(report.format_table())

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
