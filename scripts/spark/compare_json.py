"""Compare two parity payloads (or any two JSON files) within a tolerance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import compare_json, subtree  # noqa: E402

MACHINE_PATH = "identity.machine"


def _plain_json(value: object) -> str:
    """Canonical text of a JSON value: equal text means equal data, with no tolerance."""
    return json.dumps(value, sort_keys=True)


def _differ_failures(left: object, right: object, paths: list[str]) -> list[str]:
    """One message per ``--differ`` path that fails: missing on a side, or equal on both."""
    failures: list[str] = []
    for path in paths:
        found: dict[str, object] = {}
        for side, payload in (("left", left), ("right", right)):
            try:
                found[side] = subtree(payload, path)
            except KeyError:
                failures.append(f"{path} is missing on {side}")
        if len(found) == 2 and _plain_json(found["left"]) == _plain_json(found["right"]):
            failures.append(f"{path} is identical on both sides")
    return failures


def _machine_block(payload: object) -> object:
    """The payload's ``identity.machine`` block, or None when it has none."""
    try:
        return subtree(payload, MACHINE_PATH)
    except KeyError:
        return None


def _describe_machine(block: object) -> str:
    """``machine/system/pyX.Y.Z`` from the keys ``machine_info()`` writes; 'absent' if none."""
    if not isinstance(block, dict):
        return "absent"
    parts = [str(block[key]) for key in ("machine", "system") if key in block]
    if "python" in block:
        parts.append(f"py{block['python']}")
    return "/".join(parts) or "no machine/system/python keys"


def _print_machine_lines(left: object, right: object) -> None:
    """One line naming both machines, then a WARNING per side whose tree was dirty."""
    blocks = {"left": _machine_block(left), "right": _machine_block(right)}
    if any(isinstance(block, dict) for block in blocks.values()):
        print(
            f"machines: left {_describe_machine(blocks['left'])} "
            f"vs right {_describe_machine(blocks['right'])}"
        )
    for side, block in blocks.items():
        if isinstance(block, dict) and block.get("git_dirty") is True:
            print(f"WARNING: {side} payload has git_dirty=true")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two JSON files within a tolerance.")
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--rtol", type=float, default=1e-9)
    parser.add_argument("--atol", type=float, default=1e-12)
    parser.add_argument("--ignore", nargs="*", default=[],
                        help="dotted paths to skip, e.g. identity.machine")
    parser.add_argument("--differ", action="append", default=[], metavar="PATH",
                        help="repeatable; fail unless BOTH payloads hold the subtree at this "
                             "dotted path and the two subtrees differ (plain JSON equality, "
                             "no tolerance; judged on the raw payloads, before --ignore)")
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
    _print_machine_lines(left, right)
    failures = _differ_failures(left, right, args.differ)
    if result.n_numeric < args.min_numeric:
        failures.append(
            f"only {result.n_numeric} numeric leaf/leaves compared (floor {args.min_numeric})"
        )
    if not result.ok:
        failures.append(f"{len(result.mismatches)} mismatch(es)")
    for failure in failures:
        print(f"PARITY FAILED: {failure}")
    if failures:
        return 1
    print("PARITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
