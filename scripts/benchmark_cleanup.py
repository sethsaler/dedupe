#!/usr/bin/env python3
"""Read-only cold/warm scan benchmark, with a private disposable hash cache."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dedupe.engine import run_scan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--semantic", action="store_true")
    args = parser.parse_args()
    reports = {}
    last_update = 0.0

    def progress(status):
        nonlocal last_update
        if time.monotonic() - last_update >= 5:
            print(f"{mode}: {status.phase}, {status.files_processed} processed, "
                  f"{status.groups_found} groups", file=sys.stderr, flush=True)
            last_update = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="dedupe-benchmark-") as temporary:
        for mode in ("cold", "warm"):
            result = run_scan(args.roots, cache_path=Path(temporary) / "cache.sqlite3",
                              semantic=args.semantic, semantic_duplicate_threshold=0.94,
                              find_low_resolution=False, random_review_count=0,
                              progress=progress)
            reports[mode] = {"files": len(result.files), "groups": len(result.groups),
                             "reclaimable_bytes": result.reclaimable_bytes,
                             "diagnostics": result.diagnostics.to_dict(), "errors": result.errors}
            args.json.write_text(json.dumps(reports, indent=2) + "\n")
    # run_scan only inventories and analyzes. It does not execute deletions.
    args.json.write_text(json.dumps(reports, indent=2) + "\n")
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
