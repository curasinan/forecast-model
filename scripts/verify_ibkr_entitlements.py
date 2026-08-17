"""Stage 0 entitlement verification gate for the IBKR ForecastEx migration.

Standalone diagnostic. Connects to a running IB Gateway or TWS session and
answers, empirically, the questions that gate the rest of the migration:

  1. Which account(s) this session holds, account type, and base currency
     (legal entity must be confirmed in Client Portal; the API does not
     expose it directly -- the report says what evidence was observed).
  2. Whether event-contract / prediction-market trading permissions appear
     to be enabled (inferred from contract discovery + whatIf order probes).
  3. Raw contract specification fields for at least three live ForecastEx
     contracts, resolved via reqContractDetails -- never hardcoded.
  4. The discovered contract universe with count (reported as a lower
     bound, with the discovery method noted).
  5. Paper/live contract-set parity (via --snapshot on one session and
     --compare on the other).
  6. Which order types are accepted (contract metadata `orderTypes` plus
     whatIfOrder probes for LMT / MKT / conditional; whatIf never
     transmits an order).

Usage:
    # First run (e.g. against the paper Gateway on port 4002):
    python scripts/verify_ibkr_entitlements.py --port 4002

    # Live session, saving a universe snapshot:
    python scripts/verify_ibkr_entitlements.py --port 4001 --snapshot data/ibkr_universe_live.json

    # Paper session, comparing against the live snapshot:
    python scripts/verify_ibkr_entitlements.py --port 4002 --compare data/ibkr_universe_live.json

Exit codes:
    0  PASS
    1  connection or unexpected failure
    2  event-contract permissions appear to be absent
    3  zero prediction-market contracts discovered
    4  paper session does not expose the (compared) live contract set

Notes:
    - Requires a running, logged-in IB Gateway (ports 4001 live / 4002 paper)
      or TWS (7496 live / 7497 paper) with API socket clients enabled.
    - The whatIf probes require "Read-Only API" to be UNCHECKED in the
      Gateway/TWS API settings. whatIf orders are margin/commission checks
      only; nothing is ever transmitted to the exchange.
    - Uses delayed market data (type 4) so no paid subscription is needed
      for the YES+NO price-sum check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    # ib_async uses raw sockets; the selector loop is the safe choice on Windows.
    # The policy API is deprecated (removal in 3.16) but still the only way to
    # steer ib_async's internally created loop; revisit when ib_async does.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from ib_async import IB, Contract, LimitOrder, MarketOrder, ScannerSubscription
except ImportError:
    print("FATAL: ib_async is not installed. Run: pip install ib_async")
    sys.exit(1)

# Probe candidates. These are PROBES, not assumptions: the script reports which
# of them the API actually answers to. Discovered values win; these lists only
# seed the search.
CANDIDATE_EXCHANGES = ["FORECASTX", "IBEOS"]
CANDIDATE_SEC_TYPES = ["EVENT", "EC", "FUT", "OPT", "IND", "CONTFUT", "CMDTY"]
SEED_SEARCH_TERMS = [
    "Fed Funds", "CPI", "GDP", "unemployment", "inflation", "FOMC",
    "recession", "global temperature", "carbon", "hurricane", "initial claims",
]

DEFAULT_PORTS = [4002, 4001, 7497, 7496]  # GW paper, GW live, TWS paper, TWS live
INFO_ERROR_CODES = {2104, 2106, 2107, 2108, 2119, 2158}  # data-farm status noise
PERMISSION_ERROR_HINTS = (
    "permission", "not allowed", "restricted", "no trading", "not available for",
    "does not have trading", "entitle",
)

CONTRACT_FIELDS = (
    "conId", "symbol", "localSymbol", "secType", "exchange", "primaryExchange",
    "currency", "tradingClass", "multiplier", "lastTradeDateOrContractMonth",
    "right", "strike",
)
DETAIL_FIELDS = (
    "marketName", "minTick", "orderTypes", "validExchanges", "longName",
    "contractMonth", "timeZoneId", "underConId", "evRule", "evMultiplier",
)


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def add(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def heading(self, text: str) -> None:
        self.add("")
        self.add(f"## {text}")
        self.add("")

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = [
            "# Stage 0 entitlement report",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            "Tool: scripts/verify_ibkr_entitlements.py",
            "",
        ]
        appendix = ["", "## Appendix: API errors/warnings observed", ""]
        for e in self.errors:
            appendix.append(f"- reqId={e['reqId']} code={e['code']}: {e['msg']}")
        path.write_text("\n".join(header + self.lines + appendix), encoding="utf-8")


def contract_row(c: Contract, details=None) -> dict:
    row = {f: getattr(c, f, None) for f in CONTRACT_FIELDS}
    if details is not None:
        row.update({f: getattr(details, f, None) for f in DETAIL_FIELDS})
    return row


def fmt_dict(d: dict, indent: str = "    ") -> str:
    return "\n".join(f"{indent}{k} = {v!r}" for k, v in d.items() if v not in (None, "", 0.0) or k in ("conId", "minTick"))


def connect(host: str, port: int | None, client_id: int, rep: Report) -> tuple[IB, int]:
    # Without this, any request dropped during a Gateway<->IBKR connectivity
    # blip (error 1100) blocks its sync wrapper forever and hangs the script.
    IB.RequestTimeout = 60
    ports = [port] if port else DEFAULT_PORTS
    last_exc: Exception | None = None
    for p in ports:
        ib = IB()

        def on_error(reqId, errorCode, errorString, contract=None, *args):
            if errorCode not in INFO_ERROR_CODES:
                rep.errors.append({"reqId": reqId, "code": errorCode, "msg": errorString})

        ib.errorEvent += on_error
        try:
            ib.connect(host, p, clientId=client_id, timeout=8, readonly=False)
            return ib, p
        except Exception as exc:  # noqa: BLE001 - report and try next port
            last_exc = exc
            ib.disconnect()
    rep.add("FATAL: could not connect to IB Gateway/TWS on "
            f"{host} ports {ports}.")
    rep.add("Checklist:")
    rep.add("  - Is IB Gateway or TWS running and logged in on this machine?")
    rep.add("  - API enabled? Configure -> Settings -> API -> Settings ->")
    rep.add("    'Enable ActiveX and Socket Clients', trusted IP 127.0.0.1.")
    rep.add("  - Correct port? Gateway: 4001 live / 4002 paper. TWS: 7496 / 7497.")
    rep.add(f"  - Last error: {last_exc!r}")
    sys.exit(1)


def section_account(ib: IB, rep: Report) -> dict:
    rep.heading("1. Session and account facts")
    accounts = ib.managedAccounts()
    rep.add(f"Managed accounts: {accounts}")
    server_version = ib.client.serverVersion()
    rep.add(f"Server version: {server_version}")
    modes = {}
    for acct in accounts:
        # Prefix is the only API-visible paper/live signal: DU/DF = paper.
        modes[acct] = "paper" if acct.startswith(("DU", "DF")) else "live"
    rep.add(f"Account modes (by prefix): {modes}")
    summary_rows = []
    try:
        for av in ib.accountSummary():
            summary_rows.append((av.account, av.tag, av.value, av.currency))
    except Exception as exc:  # noqa: BLE001
        rep.add(f"accountSummary failed: {exc!r}")
    for acct, tag, value, currency in summary_rows:
        if tag in ("AccountType", "NetLiquidation", "TotalCashValue",
                   "AvailableFunds", "Currency", "AccruedCash"):
            rep.add(f"  {acct}  {tag} = {value} {currency}")
    rep.add("")
    rep.add("NOTE: the servicing IBKR legal entity is not exposed via the API.")
    rep.add("Confirm it in Client Portal (Settings -> Account Settings) and")
    rep.add("record it in docs/venue_semantics.md. Turkey-resident accounts are")
    rep.add("typically held by a non-US entity; product availability follows the")
    rep.add("entity, which is exactly what the probes below measure.")
    return {"accounts": accounts, "modes": modes, "summary": summary_rows}


def chain_via_secdef(ib: IB, sym: str, rep: Report, found: dict[int, dict]) -> None:
    """Resolve a symbol's event-contract chain via reqSecDefOptParams.

    Used when the direct OPT-wildcard chain request gets no response (observed
    2026-08-16 for FF, CPI and others). Resolves the underlying IND first, asks
    for the option parameters, then resolves a sample of concrete contracts.
    """
    try:
        ind = ib.reqContractDetails(Contract(symbol=sym, secType="IND", exchange="FORECASTX"))
    except Exception:  # noqa: BLE001
        ind = []
    if not ind:
        rep.add(f"  {sym}: no IND definition on FORECASTX; secdef fallback impossible")
        return
    ucon = ind[0].contract
    found[ucon.conId] = contract_row(ucon, ind[0])
    try:
        params = ib.reqSecDefOptParams(ucon.symbol, "", ucon.secType, ucon.conId)
    except Exception as exc:  # noqa: BLE001
        rep.add(f"  {sym}: reqSecDefOptParams failed: {exc!r}")
        return
    if not params:
        rep.add(f"  {sym}: secdef reports NO option chain (likely nothing listed now)")
        return
    for p in params:
        implied = 2 * len(p.expirations) * len(p.strikes)
        rep.add(f"  {sym}: secdef chain exch={p.exchange} tradingClass={p.tradingClass} "
                f"expirations={len(p.expirations)} strikes={len(p.strikes)} "
                f"(~{implied} contracts implied)")
        for exp in sorted(p.expirations)[:2]:
            for strike in sorted(p.strikes)[:2]:
                for right in ("C", "P"):
                    try:
                        det = ib.reqContractDetails(Contract(
                            symbol=sym, secType="OPT", exchange="FORECASTX",
                            lastTradeDateOrContractMonth=exp,
                            strike=strike, right=right))
                    except Exception:  # noqa: BLE001
                        det = []
                    for d in det:
                        found[d.contract.conId] = contract_row(d.contract, d)
                    time.sleep(0.2)


def discover_universe(ib: IB, rep: Report, only_symbols: list[str] | None = None,
                      chain_timeout: int = 60) -> dict[int, dict]:
    """Multi-probe discovery. Returns {conId: contract_row}."""
    rep.heading("2. Contract discovery (multi-probe, nothing hardcoded)")
    found: dict[int, dict] = {}
    responding: set[tuple[str, str]] = set()

    if only_symbols:
        # Follow-up mode: skip the broad probes; go secdef-first per symbol,
        # falling back to the slow OPT wildcard only if secdef finds nothing.
        rep.add(f"Follow-up mode: chains only, for {only_symbols}")
        prev = IB.RequestTimeout
        for sym in only_symbols:
            print(f"  [secdef] {sym} ...", flush=True)
            before = len(found)
            chain_via_secdef(ib, sym, rep, found)
            if len(found) == before:
                IB.RequestTimeout = chain_timeout
                print(f"  [chain] {sym}/OPT (fallback) ...", flush=True)
                try:
                    details = ib.reqContractDetails(
                        Contract(symbol=sym, secType="OPT", exchange="FORECASTX"))
                except Exception as exc:  # noqa: BLE001
                    print(f"  [chain] {sym}/OPT -> {type(exc).__name__}", flush=True)
                    details = []
                for d in details:
                    found[d.contract.conId] = contract_row(d.contract, d)
                IB.RequestTimeout = prev
            time.sleep(0.3)
        tradable = {cid for cid, r in found.items() if r.get("secType") != "IND"}
        rep.add("")
        rep.add(f"Distinct contracts discovered (lower bound): {len(found)}")
        rep.add(f"  of which tradable (non-IND): {len(tradable)}")
        return found

    # Probe A: wildcard reqContractDetails per (exchange, secType).
    # Observed 2026-08-16: these all time out (no response at all) — keep them
    # as cheap probes with a short timeout in case the API behavior changes.
    rep.add("Probe A: reqContractDetails wildcards per (exchange, secType)")
    prev_timeout = IB.RequestTimeout
    IB.RequestTimeout = 10
    for exchange in CANDIDATE_EXCHANGES:
        for sec_type in CANDIDATE_SEC_TYPES:
            print(f"  [probe] {exchange}/{sec_type} ...", flush=True)
            try:
                details = ib.reqContractDetails(
                    Contract(secType=sec_type, exchange=exchange))
            except Exception as exc:  # noqa: BLE001
                print(f"  [probe] {exchange}/{sec_type} -> {type(exc).__name__}", flush=True)
                details = []
            if details:
                responding.add((exchange, sec_type))
                rep.add(f"  {exchange}/{sec_type}: {len(details)} contracts")
                for d in details:
                    found[d.contract.conId] = contract_row(d.contract, d)
            time.sleep(0.3)

    IB.RequestTimeout = prev_timeout

    # Probe B: symbol search on macro seed terms, then resolve matches.
    rep.add("Probe B: reqMatchingSymbols on seed terms")
    for term in SEED_SEARCH_TERMS:
        print(f"  [search] {term!r} ...", flush=True)
        try:
            matches = ib.reqMatchingSymbols(term)
        except Exception:  # noqa: BLE001
            matches = []
        time.sleep(1.1)  # reqMatchingSymbols is limited to ~1/sec
        for m in matches or []:
            c = m.contract
            hit_exchange = (c.primaryExchange or c.exchange or "").upper()
            if not any(x in hit_exchange for x in CANDIDATE_EXCHANGES):
                continue
            try:
                details = ib.reqContractDetails(
                    Contract(symbol=c.symbol, secType=c.secType,
                             exchange=c.primaryExchange or c.exchange))
            except Exception:  # noqa: BLE001
                details = []
            for d in details:
                if d.contract.conId not in found:
                    found[d.contract.conId] = contract_row(d.contract, d)
            time.sleep(0.3)

    # Probe C: scanner parameters, in case the wildcard route is closed.
    rep.add("Probe C: reqScannerParameters scan for prediction-market codes")
    try:
        xml = ib.reqScannerParameters()
        codes = sorted(set(re.findall(
            r"<instrument>[^<]*</instrument>", xml)))
        pm_codes = [c for c in codes if re.search(
            r"FORECAST|EVENT|IBEOS", c, re.IGNORECASE)]
        rep.add(f"  scanner instrument codes matching prediction markets: {pm_codes or 'none'}")
        for code_tag in pm_codes:
            code = re.sub(r"</?instrument>", "", code_tag)
            for location in (code, f"{code}.US", "EVENT.US"):
                try:
                    sub = ScannerSubscription(
                        instrument=code, locationCode=location,
                        scanCode="MOST_ACTIVE")
                    rows = ib.reqScannerData(sub)
                    for r in rows or []:
                        d = r.contractDetails
                        found[d.contract.conId] = contract_row(d.contract, d)
                    if rows:
                        break
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        rep.add(f"  scanner probe failed: {exc!r}")

    # Probe D: the tradable event contracts are derivatives on the discovered
    # underlyings (observed 2026-08-16: reqMktDepthExchanges lists OPT and FOP
    # on FORECASTX, and the IND definitions reject orders with error 10345).
    # Resolve the per-underlying chains by symbol wildcard.
    underlying_syms = sorted({
        r["symbol"] for r in found.values() if r.get("secType") == "IND" and r.get("symbol")})
    rep.add(f"Probe D: derivative chains for {len(underlying_syms)} underlyings")
    for sym in underlying_syms:
        got = 0
        for sec_type in ("OPT", "FOP"):
            print(f"  [chain] {sym}/{sec_type} ...", flush=True)
            try:
                details = ib.reqContractDetails(
                    Contract(symbol=sym, secType=sec_type, exchange="FORECASTX"))
            except Exception as exc:  # noqa: BLE001
                print(f"  [chain] {sym}/{sec_type} -> {type(exc).__name__}", flush=True)
                details = []
            for d in details:
                found[d.contract.conId] = contract_row(d.contract, d)
            got += len(details)
            time.sleep(0.3)
            if details:
                break  # chain found under this secType; no need to probe the next
        rep.add(f"  {sym}: {got} derivative contracts")

    tradable = {cid for cid, r in found.items() if r.get("secType") != "IND"}
    rep.add("")
    rep.add(f"Responding (exchange, secType) combinations: {sorted(responding) or 'NONE'}")
    rep.add(f"Distinct contracts discovered (lower bound): {len(found)}")
    rep.add(f"  of which tradable (non-IND): {len(tradable)}")
    return found


def section_contract_specs(found: dict[int, dict], rep: Report) -> None:
    rep.heading("3. Raw contract specifications (first 3+, resolved via reqContractDetails)")
    if not found:
        rep.add("No contracts to show.")
        return
    for i, (con_id, row) in enumerate(sorted(found.items())):
        if i >= 5:
            rep.add(f"... plus {len(found) - 5} more (full list in the JSON snapshot)")
            break
        rep.add(f"Contract conId={con_id}:")
        rep.add(fmt_dict(row))
        rep.add("")


def section_yes_no_structure(ib: IB, found: dict[int, dict], rep: Report) -> None:
    rep.heading("4. Yes/No structure and price-sum check")
    pool = {cid: r for cid, r in found.items() if r.get("secType") != "IND"} or found
    # Group by (symbol, expiry, strike): a paired venue shows 2 conIds per
    # group, typically distinguished by right (C=Yes / P=No on ForecastEx).
    groups: dict[tuple, list[int]] = {}
    for con_id, row in pool.items():
        key = (row.get("symbol"), row.get("lastTradeDateOrContractMonth"),
               row.get("strike"))
        groups.setdefault(key, []).append(con_id)
    sizes = sorted({len(v) for v in groups.values()})
    rep.add(f"Tradable contracts per (symbol, expiry, strike) group -- distinct sizes: {sizes}")
    rep.add("  (2 => Yes and No are separate conIds; 1 => one contract, two sides)")
    rights = sorted({str(r.get("right")) for r in pool.values()})
    rep.add(f"Distinct 'right' values across tradable contracts: {rights}")
    sample_key = max(groups, key=lambda k: len(groups[k]))
    sample_ids = groups[sample_key][:2]
    rep.add(f"Sample group {sample_key}: conIds {groups[sample_key]}")
    for cid in sample_ids:
        row = pool[cid]
        rep.add(f"  conId={cid}: right={row.get('right')!r} strike={row.get('strike')!r} "
                f"localSymbol={row.get('localSymbol')!r} "
                f"tradingClass={row.get('tradingClass')!r}")
    # Delayed quotes for the sample pair -> empirical YES+NO sum.
    try:
        ib.reqMarketDataType(4)  # delayed-frozen; no paid subscription needed
        contracts = []
        for cid in sample_ids:
            c = Contract(conId=cid, exchange=found[cid].get("exchange") or "")
            contracts.append(c)
        ib.qualifyContracts(*contracts)
        tickers = ib.reqTickers(*contracts)
        quotes = {}
        for t in tickers:
            quotes[t.contract.conId] = {
                "bid": t.bid, "ask": t.ask, "last": t.last, "close": t.close}
            rep.add(f"  quote conId={t.contract.conId}: bid={t.bid} ask={t.ask} "
                    f"last={t.last} close={t.close}")
        if len(quotes) == 2:
            asks = [q["ask"] for q in quotes.values()]
            bids = [q["bid"] for q in quotes.values()]
            if all(isinstance(x, (int, float)) and x == x and x > 0 for x in asks):
                rep.add(f"  Sum of the two ASK prices: {sum(asks):.4f} "
                        "(spec expects ~1.01 on ForecastEx; verify)")
            if all(isinstance(x, (int, float)) and x == x and x > 0 for x in bids):
                rep.add(f"  Sum of the two BID prices: {sum(bids):.4f}")
    except Exception as exc:  # noqa: BLE001
        rep.add(f"  quote check failed (fine if no market data at all): {exc!r}")


def section_order_probes(ib: IB, found: dict[int, dict], rep: Report) -> list[dict]:
    rep.heading("5. Order-type probes (whatIf only -- nothing is transmitted)")
    results: list[dict] = []
    if not found:
        rep.add("Skipped: no contracts discovered.")
        return results
    tradable = [(cid, r) for cid, r in sorted(found.items())
                if r.get("secType") != "IND"]
    con_id, row = tradable[0] if tradable else next(iter(sorted(found.items())))
    contract = Contract(conId=con_id, exchange=row.get("exchange") or "")
    try:
        ib.qualifyContracts(contract)
    except Exception as exc:  # noqa: BLE001
        rep.add(f"qualifyContracts failed for conId={con_id}: {exc!r}")
        return results
    rep.add(f"Venue-declared orderTypes for conId={con_id}: {row.get('orderTypes')!r}")

    min_tick = row.get("minTick") or 0.01
    probe_price = round(round(0.50 / min_tick) * min_tick, 4)
    # Explicit tif on every order: without it the venue's order preset rewrites
    # TIF and emits error 10349, which aborts the whatIf round-trip (observed
    # 2026-08-16). The GTC probe answers "are resting limit orders permitted".
    orders = [("LMT DAY", LimitOrder("BUY", 1, probe_price, tif="DAY")),
              ("LMT GTC (resting)", LimitOrder("BUY", 1, probe_price, tif="GTC")),
              ("MKT DAY", MarketOrder("BUY", 1, tif="DAY"))]
    try:
        from ib_async.order import PriceCondition
        cond_order = LimitOrder("BUY", 1, probe_price, tif="DAY")
        cond_order.conditions = [PriceCondition(
            price=probe_price, conId=con_id,
            exch=row.get("exchange") or "SMART")]
        orders.append(("LMT+PriceCondition", cond_order))
    except Exception:  # noqa: BLE001
        rep.add("  (conditional-order probe unavailable in this ib_async version)")

    for label, order in orders:
        order.whatIf = True
        errors_before = len(rep.errors)
        state = None
        try:
            state = ib.whatIfOrder(contract, order)
        except Exception as exc:  # noqa: BLE001
            rep.add(f"  {label}: EXCEPTION {exc!r}")
        new_errors = rep.errors[errors_before:]
        if state is not None and getattr(state, "initMarginChange", "") not in ("", None):
            rep.add(f"  {label}: ACCEPTED  commission={getattr(state, 'commission', None)!r} "
                    f"minCommission={getattr(state, 'minCommission', None)!r} "
                    f"maxCommission={getattr(state, 'maxCommission', None)!r} "
                    f"commissionCurrency={getattr(state, 'commissionCurrency', '')!r} "
                    f"initMarginChange={state.initMarginChange!r} "
                    f"warning={getattr(state, 'warningText', '')!r}")
            results.append({"type": label, "accepted": True})
        else:
            msgs = "; ".join(f"{e['code']}: {e['msg']}" for e in new_errors) or "no OrderState returned"
            rep.add(f"  {label}: REJECTED/UNKNOWN  ({msgs})")
            rep.add("     If this says read-only: uncheck 'Read-Only API' in the")
            rep.add("     Gateway/TWS API settings and rerun. whatIf never transmits.")
            results.append({"type": label, "accepted": False, "detail": msgs})
    return results


def section_depth(ib: IB, rep: Report) -> None:
    rep.heading("6. Market depth availability")
    try:
        rows = ib.reqMktDepthExchanges()
        hits = [r for r in rows
                if any(x in (r.exchange or "").upper() for x in CANDIDATE_EXCHANGES)]
        if hits:
            for r in hits:
                rep.add(f"  {r.exchange}  secType={r.secType} "
                        f"listingExch={getattr(r, 'listingExch', '')} "
                        f"aggGroup={getattr(r, 'aggGroup', '')}")
        else:
            rep.add("  No prediction-market exchange offers depth-of-book via "
                    "reqMktDepth (top-of-book only). Recorder design follows this.")
    except Exception as exc:  # noqa: BLE001
        rep.add(f"  reqMktDepthExchanges failed: {exc!r}")


def permission_verdict(found: dict, order_results: list[dict], rep: Report) -> int:
    """Returns the exit code contribution: 0 ok, 2 no permission, 3 no contracts."""
    rep.heading("7. Verdict")
    tradable_count = sum(1 for r in found.values() if r.get("secType") != "IND")
    if not found or tradable_count == 0:
        rep.add("FAIL(3): no TRADABLE prediction-market contracts discovered "
                f"({len(found)} non-tradable index definitions visible).")
        rep.add("Either the account entity does not offer them, permissions are not")
        rep.add("enabled, or the probe set missed the venue. Check Client Portal ->")
        rep.add("Settings -> Trading Permissions -> Prediction Markets, then rerun.")
        return 3
    permission_errors = [
        e for e in rep.errors
        if any(h in e["msg"].lower() for h in PERMISSION_ERROR_HINTS)
    ]
    any_accepted = any(r.get("accepted") for r in order_results)
    if permission_errors and not any_accepted:
        rep.add("FAIL(2): contracts are visible but every order probe was rejected")
        rep.add("with permission-style errors:")
        for e in permission_errors:
            rep.add(f"  - {e['code']}: {e['msg']}")
        return 2
    rep.add(f"PASS: {len(found)} contracts discovered ({tradable_count} tradable); "
            f"order probes accepted: {[r['type'] for r in order_results if r.get('accepted')]}")
    return 0


def compare_snapshots(found: dict[int, dict], compare_path: Path, rep: Report) -> int:
    rep.heading("8. Paper/live contract-set comparison")
    other = json.loads(compare_path.read_text(encoding="utf-8"))
    other_ids = set(other["con_ids"])
    these_ids = set(found.keys())
    missing_here = other_ids - these_ids
    extra_here = these_ids - other_ids
    rep.add(f"Reference snapshot: {compare_path} "
            f"(mode={other.get('mode')}, {len(other_ids)} contracts, "
            f"taken {other.get('timestamp')})")
    rep.add(f"This session: {len(these_ids)} contracts")
    rep.add(f"Missing here vs reference: {len(missing_here)}")
    rep.add(f"Extra here vs reference: {len(extra_here)}")
    if other_ids and (not these_ids or len(missing_here) / len(other_ids) > 0.5):
        rep.add("FAIL(4): this session lacks most of the reference contract set.")
        rep.add("Per the migration spec this blocks Stage 7 (native paper trading);")
        rep.add("the custom simulator fed by the Stage 5 recorder is the fallback.")
        return 4
    rep.add("OK: contract sets are materially the same.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None,
                    help="TWS API port; default probes 4002, 4001, 7497, 7496")
    ap.add_argument("--client-id", type=int, default=3,
                    help="3 = research per the migration client-id map")
    ap.add_argument("--snapshot", type=Path, default=None,
                    help="write discovered universe (conIds + specs) to this JSON")
    ap.add_argument("--compare", type=Path, default=None,
                    help="compare this session's universe to a prior --snapshot")
    ap.add_argument("--report", type=Path, default=None,
                    help="report path (default docs/stage0_report_<date>_<mode>.md)")
    ap.add_argument("--only-symbols", default=None,
                    help="comma-separated symbols: skip broad probes, resolve just "
                         "these chains (secdef-first follow-up mode)")
    ap.add_argument("--chain-timeout", type=int, default=60,
                    help="seconds before an unanswered chain request is abandoned")
    ap.add_argument("--probe-conid", type=int, default=None,
                    help="also run the order probes against this known conId")
    args = ap.parse_args()

    rep = Report()
    rep.add("Stage 0: IBKR ForecastEx entitlement verification")
    rep.add(f"Started: {datetime.now(timezone.utc).isoformat()}")

    ib, port = connect(args.host, args.port, args.client_id, rep)
    rep.add(f"Connected: {args.host}:{port} clientId={args.client_id}")
    exit_code = 0
    mode = "unknown"
    try:
        acct = section_account(ib, rep)
        mode = next(iter(acct["modes"].values()), "unknown")
        only = ([s.strip().upper() for s in args.only_symbols.split(",") if s.strip()]
                if args.only_symbols else None)
        found = discover_universe(ib, rep, only_symbols=only,
                                  chain_timeout=args.chain_timeout)
        if args.probe_conid:
            try:
                for d in ib.reqContractDetails(
                        Contract(conId=args.probe_conid, exchange="FORECASTX")):
                    found[d.contract.conId] = contract_row(d.contract, d)
            except Exception as exc:  # noqa: BLE001
                rep.add(f"--probe-conid {args.probe_conid} resolution failed: {exc!r}")
        section_contract_specs(found, rep)
        section_yes_no_structure(ib, found, rep)
        order_results = section_order_probes(ib, found, rep)
        section_depth(ib, rep)
        exit_code = permission_verdict(found, order_results, rep)
        if args.compare and exit_code == 0:
            exit_code = compare_snapshots(found, args.compare, rep)
        if args.snapshot:
            args.snapshot.parent.mkdir(parents=True, exist_ok=True)
            args.snapshot.write_text(json.dumps({
                "mode": mode,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "con_ids": sorted(found.keys()),
                "contracts": {str(k): v for k, v in found.items()},
            }, indent=2, default=str), encoding="utf-8")
            rep.add(f"\nSnapshot written: {args.snapshot}")
    finally:
        ib.disconnect()

    report_path = args.report or Path(
        f"docs/stage0_report_{datetime.now(timezone.utc):%Y-%m-%d}_{mode}.md")
    rep.write(report_path)
    rep.add("")
    rep.add(f"Report written: {report_path}")
    rep.add(f"Exit code: {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
