"""Manifest the operational files under a quant-data root (sha256, size, mtime)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import HEARTBEAT_REL, operational_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manifest the operational files of a root.")
    parser.add_argument("--root", required=True, help="quant-data root, e.g. ~/quant_data")
    parser.add_argument("--out", required=True, help="where to write the manifest JSON")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    manifest = operational_manifest(root)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    heartbeat = root.joinpath(*HEARTBEAT_REL)
    hb_mtime = heartbeat.stat().st_mtime_ns if heartbeat.exists() else None
    present = sum(1 for v in manifest.values() if v is not None)
    print(f"manifest: {present}/{len(manifest)} files present, heartbeat mtime_ns={hb_mtime} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
