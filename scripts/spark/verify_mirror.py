"""Verify a mirrored quant-data root and print its snapshot identity.

    python scripts/spark/verify_mirror.py --root ~/quant_data

Exit 0: every canonical file opens; stdout is the identity JSON (design
section 5.1). Exit 1: stderr lists every unreadable or missing file --
never a silent skip. Read-only; safe on the live Zenbook root too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import snapshot_identity, verify_mirror  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a mirrored quant-data root.")
    parser.add_argument("--root", required=True, help="quant-data root, e.g. ~/quant_data")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    failures = verify_mirror(root)
    for line in failures:
        print(f"UNREADABLE {line}", file=sys.stderr)
    if failures:
        print(f"verify_mirror: {len(failures)} problem(s) under {root}", file=sys.stderr)
        return 1
    print(json.dumps(snapshot_identity(root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
