"""A23 Deployment Manifest Generator CLI.

Usage:
  python scripts/generate_deployment_manifest.py [--output ops/a23_deployment_manifest.json] [--verify]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add agent/src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from manifest import generate_deployment_manifest, verify_deployment_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="A23 Deployment Manifest CLI")
    parser.add_argument("--output", "-o", default="ops/a23_deployment_manifest.json", help="Manifest output path")
    parser.add_argument("--verify", action="store_true", help="Verify existing manifest against current worktree")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    out_path = repo_root / args.output

    if args.verify:
        if not out_path.exists():
            print(f"ERROR: Manifest not found at {out_path}")
            return 1
        valid, drift = verify_deployment_manifest(out_path, repo_root)
        if valid:
            print(f"[OK] Deployment manifest {out_path} is VALID (0 drift).")
            return 0
        else:
            print(f"[FAIL] Deployment manifest {out_path} FAILED verification:")
            for d in drift:
                print(f"  - {d}")
            return 1

    manifest = generate_deployment_manifest(repo_root, out_path)
    print(f"[OK] Generated deployment manifest: {out_path}")
    print(f"  Signature: {manifest['manifest_signature']}")
    print(f"  Commit:    {manifest['main_commit_sha']}")
    print(f"  Files:     {len(manifest['core_file_hashes'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
