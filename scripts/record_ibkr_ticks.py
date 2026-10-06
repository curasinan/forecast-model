"""Record ForecastEx delayed-quote sweeps to parquet.

Thin wrapper around src.venues.ibkr.recorder.QuoteRecorder. Settings are
env-driven: IBKR_PORT and IBKR_TRADING_MODE MUST be set (environment or
.env) -- IBKRSettings has no defaults for them by design and construction
fails loudly otherwise (migration spec Stage 7).

Usage:
    python scripts/record_ibkr_ticks.py --once            # single sweep, then exit
    python scripts/record_ibkr_ticks.py                   # loop every IBKR_RECORD_INTERVAL_MINUTES
    python scripts/record_ibkr_ticks.py --interval-minutes 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402
from src.venues.ibkr.recorder import QuoteRecorder, setup_logging  # noqa: E402
from src.venues.ibkr.settings import IBKRSettings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="run a single sweep, then exit")
    ap.add_argument("--interval-minutes", type=int, default=None,
                    help="override IBKR_RECORD_INTERVAL_MINUTES for this run")
    ap.add_argument("--snapshot", default="data/ibkr_universe_live.json",
                    help="universe snapshot used to seed an empty contract cache")
    args = ap.parse_args()

    settings = IBKRSettings()  # loud ValidationError if IBKR_PORT / IBKR_TRADING_MODE unset
    if args.interval_minutes is not None:
        settings.record_interval_minutes = args.interval_minutes

    try:
        refuse_if_mirror(settings.resolved_data_root, "record quotes")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2

    setup_logging()
    print(f"gateway: {settings.host}:{settings.port} mode={settings.trading_mode}")
    print(f"interval: {settings.record_interval_minutes} minutes"
          + (" (--once: single sweep)" if args.once else ""))
    print(f"quotes -> {settings.resolved_data_root / 'quotes'}")
    print(f"state  -> {settings.resolved_data_root / 'state' / 'recorder_state.json'}")
    print("log    -> logs/ibkr_recorder.log")

    recorder = QuoteRecorder(settings, snapshot_path=args.snapshot)
    recorder.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
