"""Parity probe: score a frozen quant-data snapshot with the operational model.

    python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out ~/quant_data_snapshot/parity/zenbook.json
    python scripts/spark/parity_score.py --root ~/quant_data --out ~/parity_spark.json   # on the Spark

Research-only (design section 5.2): never writes paper/gtm_signals.json or
a history line, applies no staleness gate. Compare two outputs with
scripts/spark/compare_json.py --ignore identity.machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import score_snapshot_from_root  # noqa: E402

DEFAULT_UNIVERSE = REPO_ROOT / "data" / "ibkr_universe_live.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a frozen quant-data snapshot.")
    parser.add_argument("--root", required=True, help="quant-data root (holds ibkr_ticks/ and climate_data/)")
    parser.add_argument("--out", required=True, help="where to write the payload JSON")
    parser.add_argument("--universe-json", default=str(DEFAULT_UNIVERSE),
                        help="Stage-0 universe snapshot for con_id -> local_symbol labels")
    args = parser.parse_args(argv)
    payload = score_snapshot_from_root(
        Path(args.root).expanduser(), universe_json=Path(args.universe_json).expanduser()
    )
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(
        f"ladder={payload['n_ladder']} tradeable={payload['n_tradeable']} "
        f"fingerprint={payload['model_fingerprint']} sweep={payload['sweep_ts_utc']} -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
