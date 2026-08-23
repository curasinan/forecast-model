"""Stage 5 probe: historical bar + market-depth availability for ForecastEx.

Answers venue_semantics.md questions 4 (native order book / depth) and 7
(reqHistoricalData availability and depth) empirically. Read-only,
clientId=3. A standalone Gateway diagnostic in the same class as
scripts/verify_ibkr_entitlements.py -- it imports ib_async directly and is
exempted from the TID251 venue-import ban via pyproject per-file-ignore
(the reviewable mechanism), not an inline suppression.

Usage (Gateway logged in on 127.0.0.1:4002):
    python scripts/probe_ibkr_history.py
Writes docs/stage5_probe_findings.md. Exit 3 if the Gateway is offline.
"""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ib_async import IB, Contract  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO_ROOT / "data" / "ibkr_universe_live.json"
REPORT = REPO_ROOT / "docs" / "stage5_probe_findings.md"
INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
PROBE_SYMBOLS = ("GTM", "UNR", "RGDP")
WHAT_TO_SHOW = ("TRADES", "MIDPOINT", "BID_ASK")


def _pick_contracts() -> list[dict]:
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    picks: dict[str, dict] = {}
    for row in snap["contracts"].values():
        if (row.get("secType") == "OPT" and row.get("right") == "C"
                and row.get("symbol") in PROBE_SYMBOLS):
            picks.setdefault(row["symbol"], row)
    return list(picks.values())


def _probe_contract(ib: IB, row: dict, out: list[str]) -> None:
    out.append(f"### {row['symbol']} conId={row['conId']} ({row.get('localSymbol')})")
    contract = Contract(conId=row["conId"], exchange="FORECASTX")
    try:
        ib.qualifyContracts(contract)
    except Exception as exc:  # noqa: BLE001
        out.append(f"- qualify failed: {exc!r}")
        return

    for what in WHAT_TO_SHOW:
        try:
            head = ib.reqHeadTimeStamp(contract, whatToShow=what, useRTH=False)
        except Exception as exc:  # noqa: BLE001
            head = f"ERR {type(exc).__name__}"
        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime="", durationStr="1 M", barSizeSetting="1 day",
                whatToShow=what, useRTH=False, formatDate=1)
            span = (f"{len(bars)} daily bars, first={bars[0].date if bars else None}, "
                    f"last={bars[-1].date if bars else None}")
        except Exception as exc:  # noqa: BLE001
            span = f"ERR {type(exc).__name__}: {exc}"
        out.append(f"- {what}: headTimeStamp={head} | 1M/1day -> {span}")

    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="2 D", barSizeSetting="1 hour",
            whatToShow="BID_ASK", useRTH=False, formatDate=1)
        out.append(f"- BID_ASK 2D/1hour -> {len(bars)} bars")
    except Exception as exc:  # noqa: BLE001
        out.append(f"- BID_ASK 2D/1hour -> ERR {exc}")

    try:
        ticker = ib.reqMktDepth(contract, numRows=5, isSmartDepth=False)
        ib.sleep(4)
        out.append(f"- depth: bid_levels={len(ticker.domBids)} ask_levels={len(ticker.domAsks)} "
                   f"top_bids={[(d.price, d.size) for d in ticker.domBids[:3]]}")
        ib.cancelMktDepth(contract, isSmartDepth=False)
    except Exception as exc:  # noqa: BLE001
        out.append(f"- depth: ERR {type(exc).__name__}: {exc}")
    out.append("")


def main() -> int:
    out: list[str] = [
        "# Stage 5 probe findings: historical bars + market depth",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "Tool: scripts/probe_ibkr_history.py (read-only, clientId=3).",
        "Answers venue_semantics.md Q4 (depth) and Q7 (historical bars).",
        "",
    ]
    errors: list[tuple] = []
    ib = IB()
    ib.errorEvent += lambda reqId, code, msg, *a: (
        errors.append((reqId, code, msg)) if code not in INFO_CODES else None)
    IB.RequestTimeout = 60
    try:
        ib.connect("127.0.0.1", 4002, clientId=3, timeout=8, readonly=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Gateway offline ({exc}); re-run when logged in.")
        return 3

    try:
        out.append("## reqMktDepthExchanges (FORECASTX)")
        try:
            rows = [r for r in ib.reqMktDepthExchanges()
                    if "FORECAST" in (r.exchange or "").upper()]
            for r in rows:
                out.append(f"- {r.exchange} secType={r.secType} "
                           f"aggGroup={getattr(r, 'aggGroup', '')}")
            if not rows:
                out.append("- none")
        except Exception as exc:  # noqa: BLE001
            out.append(f"- reqMktDepthExchanges failed: {exc!r}")
        out.append("")
        out.append("## Per-contract probes")
        out.append("")
        for row in _pick_contracts():
            _probe_contract(ib, row, out)
        out.append("## API errors/warnings observed")
        for reqId, code, msg in errors[-20:]:
            out.append(f"- reqId={reqId} code={code}: {msg}")
    finally:
        ib.disconnect()

    REPORT.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    print(f"\nReport written: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
