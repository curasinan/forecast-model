"""Compare two parity payloads (or any two JSON files) within a tolerance.

    python scripts/spark/compare_json.py left.json right.json \
        [--rtol 1e-9] [--atol 1e-12] [--ignore identity.machine ...]

Exit 0 on parity, 1 on any mismatch (each one printed with its path).
Design: docs/plans/2026-09-30-spark-research-split-design.md section 5.3.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import compare_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two JSON files within a tolerance.")
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--rtol", type=float, default=1e-9)
    parser.add_argument("--atol", type=float, default=1e-12)
    parser.add_argument("--ignore", nargs="*", default=[],
                        help="dotted paths to skip, e.g. identity.machine")
    parser.add_argument("--min-numeric", type=int, default=1,
                        help="fail when fewer numeric leaves were compared "
                             "(two empty documents must never pass as parity)")
    args = parser.parse_args(argv)
    left = json.loads(Path(args.left).expanduser().read_text(encoding="utf-8"))
    right = json.loads(Path(args.right).expanduser().read_text(encoding="utf-8"))
    result = compare_json(left, right, rtol=args.rtol, atol=args.atol, ignore=args.ignore)
    print(
        f"numeric leaves compared: {result.n_numeric}; "
        f"max abs diff {result.max_abs:.3e}; max rel diff {result.max_rel:.3e}"
    )
    for line in result.mismatches:
        print(f"MISMATCH {line}")
    if result.n_numeric < args.min_numeric:
        print(
            f"PARITY FAILED: only {result.n_numeric} numeric leaf/leaves compared "
            f"(floor {args.min_numeric})"
        )
        return 1
    if result.ok:
        print("PARITY OK")
        return 0
    print(f"PARITY FAILED: {len(result.mismatches)} mismatch(es)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
