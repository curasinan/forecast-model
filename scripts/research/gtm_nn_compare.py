"""Print one markdown table comparing the run directories of the GTM research benchmarks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.nn.report import compare_runs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare run directories: the CRPS of nn and emos per lead and the verdict."
    )
    parser.add_argument(
        "run_dir", nargs="+", type=Path, help="run directories written by gtm_nn_benchmark.py"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits with 2 on a usage error and 0 after --help
        return int(exc.code or 0)
    try:
        table = compare_runs(args.run_dir)
    except (OSError, ValueError) as exc:
        print(f"cannot read a run directory: {exc}", file=sys.stderr)
        return 2
    print(table, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
