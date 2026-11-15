"""S2: replay recorded GTM ladders through the paper executor on a simulated venue."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from climate.ladder import (
    ladder_table,
    latest_gtm_quotes,
    target_month_for_contract,
)
from climate.nn.availability import LATE_PRINTS, _genuine_first_prints
from climate.paper import inits_for_priced_targets
from climate.paper_criteria import apply_edge_gate
from climate.sim.forecast import PointInTimeForecaster
from climate.sim.panel import Contract, QuotePanel
from climate.sim.venue import SimVenue
from src.domain.fees import FlatCommissionFeeModel
from src.trading.executor import TradingExecutor
from src.trading.gtm_intents import intents_from_payload
from src.trading.position_tracker import PositionTracker
from src.trading.reconcile import (
    drop_already_committed,
    load_open_commitments,
    reconcile_paper_book,
)
from src.trading.reprice import reprice_resting_orders
from src.trading.risk_manager import RiskLimits

CADENCES = ("daily", "release", "sweep")
ORDER_COLUMNS = [
    "order_id", "perm_id", "con_id", "question_id", "target_month", "strike", "leg",
    "lead_months", "decision_ts", "snapshot_ts", "limit", "qty", "dec_bid", "dec_ask", "dec_mid",
    "edge_at_limit", "status", "close_reason", "filled", "avg_fill", "first_fill_ts",
    "hours_to_fill", "fill_cost", "mid_1d", "mid_7d", "settled", "label", "payout", "pnl",
    "mark_bid", "mark_mid",
]
NUMERIC_ORDER_COLUMNS = [
    "strike", "lead_months", "limit", "qty", "dec_bid", "dec_ask", "dec_mid", "edge_at_limit",
    "filled", "avg_fill", "hours_to_fill", "fill_cost", "mid_1d", "mid_7d", "label", "payout",
    "pnl", "mark_bid", "mark_mid",
]
RELEASE_DECISION = time(13, 0)  # an hour after SEAS5's 12:00 UTC release on the 13th


@dataclass(frozen=True)
class Policy:
    """A limit rule: 'ask', 'ask-N' (N ticks under the ask) or 'mid'."""

    name: str
    price_policy: str
    ticks_below_ask: int

    @classmethod
    def parse(cls, text: str) -> Policy:
        text = text.strip().lower()
        if text in ("ask", "mid"):
            return cls(text, text, 0)
        head, sep, n = text.partition("-")
        if head == "ask" and sep and n.isdigit():
            return cls(text, "ask", int(n))
        raise ValueError(f"policy {text!r} is not ask, ask-N or mid")


@dataclass(frozen=True)
class ReplayConfig:
    """One cell of the replay grid: a limit rule and how the venue fills it."""

    policy: Policy
    latency: int = 1
    fill_model: str = "paper"
    reprice: bool = False
    skip_running_month: bool = False
    bankroll: float = 100.0
    max_position: float = 10.0
    max_exposure: float = 50.0
    max_group: float = 15.0
    min_edge: float = 0.05
    top: int | None = None
    max_age: timedelta = timedelta(minutes=40)
    fee: float = 0.01

    @property
    def name(self) -> str:
        parts = [self.policy.name, f"lat{self.latency}", self.fill_model]
        if self.reprice:
            parts.append("reprice")
        if self.skip_running_month:
            parts.append("norunning")
        return "_".join(parts)

    def risk_limits(self) -> RiskLimits:
        return RiskLimits(min_edge_pct=self.min_edge, max_position_usd=self.max_position,
                          max_total_exposure_usd=self.max_exposure, max_group_usd=self.max_group)


@dataclass(frozen=True)
class Settlement:
    """A target month's settling value, when it is known."""

    target_month: str
    value: float | None
    vintage: str | None
    printed_at: pd.Timestamp | None


@dataclass
class ReplayResult:
    config: ReplayConfig
    decisions: pd.DataFrame
    orders: pd.DataFrame
    months: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict
    notes: list[str] = field(default_factory=list)


# -- inputs ------------------------------------------------------------------

def decision_times(panel: QuotePanel, *, cadence: str = "daily", at: time = time(14, 0),
                   start: date | None = None, end: date | None = None) -> list[pd.Timestamp]:
    """When the replayed paper loop runs, inside the panel's span and [start, end]."""
    if cadence not in CADENCES:
        raise ValueError(f"cadence must be one of {CADENCES}")
    if panel.empty:
        return []
    first, last = panel.times[0], panel.times[-1]
    lo = max(first.date(), start) if start else first.date()
    hi = min(last.date(), end) if end else last.date()
    if cadence == "sweep":
        return [t for t in panel.times if lo <= t.date() <= hi]
    days = pd.date_range(lo, hi, freq="D")
    if cadence == "release":
        days = [d for d in days if d.day == 13]
        at = max(at, RELEASE_DECISION)
    out = [pd.Timestamp(datetime.combine(d.date(), at, tzinfo=timezone.utc)) for d in days]
    return [t for t in out if first <= t <= last + timedelta(hours=1)]


def target_months(contracts: Mapping[int, Contract], labels: pd.DataFrame | None) -> dict[int, str]:
    """conId -> target month, from the contract's local symbol, else from its expiry."""
    local = {}
    if labels is not None and len(labels):
        local = dict(zip(labels["con_id"].astype("int64"), labels["local_symbol"]))
    out = {}
    for con, c in contracts.items():
        tm, _ = target_month_for_contract(local_symbol=local.get(con), expiry=c.expiry)
        if tm:
            out[con] = tm
    return out


def stop_times(contracts: Mapping[int, Contract], target_of: Mapping[int, str],
               ledger: pd.DataFrame | None) -> dict[int, pd.Timestamp]:
    """When trading in each leg stops: the expiry date's start, earlier if the ledger says so.

    A genuine first print in the ledger was first seen by the daily collector,
    so NOAA had published within the day before: trading stops a day before
    first sight. A month with no expiry and no ledger print stops on the 7th
    of the next month. Orders cannot fill on quotes left over after the print.
    """
    seen: dict[str, pd.Timestamp] = {}
    if ledger is not None and len(ledger):
        prints = _genuine_first_prints(ledger, "gtm")
        for period, first in prints.groupby("period")["first_seen_utc"]:
            seen[str(period)] = pd.to_datetime(first, utc=True).min() - pd.Timedelta(days=1)
    for period, when in LATE_PRINTS.items():
        seen.setdefault(period, pd.Timestamp(when) - pd.Timedelta(days=1))
    out = {}
    for con, tm in target_of.items():
        candidates = []
        expiry = str(contracts[con].expiry)[:8]
        if len(expiry) == 8 and expiry.isdigit():
            candidates.append(pd.Timestamp(f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}", tz="UTC"))
        if tm in seen:
            candidates.append(seen[tm])
        if not candidates:
            nxt = pd.Period(tm, freq="M") + 1
            candidates.append(pd.Timestamp(nxt.year, nxt.month, 7, tz="UTC"))
        out[con] = min(candidates)
    return out


def settlements(target_of: Mapping[int, str], values: Mapping[str, tuple[float, str]],
                printed_at: Mapping[str, datetime]) -> dict[str, Settlement]:
    """One Settlement per target month the panel trades."""
    out = {}
    for tm in sorted(set(target_of.values())):
        value, vintage = values.get(tm, (None, None))
        when = printed_at.get(tm)
        out[tm] = Settlement(tm, value, vintage, pd.Timestamp(when) if when is not None else None)
    return out


# -- the replay ----------------------------------------------------------------

class Replay:
    """One grid cell: the paper loop at each decision time, on a fresh book and venue."""

    def __init__(self, panel: QuotePanel, contracts: Mapping[int, Contract],
                 labels: pd.DataFrame | None, forecaster: PointInTimeForecaster,
                 settlements_by_month: Mapping[str, Settlement],
                 stops: Mapping[int, pd.Timestamp], config: ReplayConfig, work_dir: Path):
        self.panel = panel
        self.contracts = dict(contracts)
        self.labels = labels if labels is not None else pd.DataFrame(
            columns=["con_id", "local_symbol", "expiry"])
        self.forecaster = forecaster
        self.settlements = dict(settlements_by_month)
        self.target_of = target_months(self.contracts, self.labels)
        self.stops = dict(stops)
        self.config = config
        self.db = str(Path(work_dir) / f"{config.name}.db")
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        PositionTracker(db_path=self.db)
        self.venue = SimVenue(panel, self.contracts, self.stops, fill_model=config.fill_model,
                              latency=config.latency, fee=config.fee)
        self.fee_model = FlatCommissionFeeModel(config.fee)
        self.order_meta: dict[str, dict] = {}
        self.settled: set[str] = set()
        self.notes: list[str] = []
        self.reconcile_aborts = 0
        self.repriced = 0

    # -- per decision ------------------------------------------------------
    def _settle_due(self, t: pd.Timestamp) -> None:
        """Close the book on every month whose value is public and known by `t`."""
        for tm, s in self.settlements.items():
            if tm in self.settled or s.value is None or s.printed_at is None or s.printed_at > t:
                continue
            cons = [c for c, m in self.target_of.items() if m == tm]
            self.venue.expire(cons, s.printed_at, "expired at the print")
            self._close_in_book(cons, s, t)
            self.venue.settle(cons)
            self.settled.add(tm)

    def _close_in_book(self, cons: Sequence[int], s: Settlement, t: pd.Timestamp) -> None:
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                for con in cons:
                    market_id = f"ibkr:{con}"
                    conn.execute(
                        "UPDATE trade_log SET status = 'expired', last_synced_utc = ? "
                        "WHERE market_id = ? AND status IN ('placed', 'partially_filled')",
                        (t.isoformat(), market_id),
                    )
                    exit_price = 1.0 if self._wins(con, s.value) else 0.0
                    conn.execute(
                        "UPDATE positions SET closed_at = ?, exit_price = ?, "
                        "realized_pnl = size * ? - cost_basis "
                        "WHERE market_id = ? AND closed_at IS NULL",
                        (t.isoformat(), exit_price, exit_price, market_id),
                    )
        finally:
            conn.close()

    def _wins(self, con: int, value: float) -> bool:
        c = self.contracts[con]
        above = value > c.strike + 1e-12
        return above if c.right == "C" else not above

    def _payload(self, t: pd.Timestamp, snap_ts: pd.Timestamp, snap: pd.DataFrame):
        quotes = latest_gtm_quotes(None, sweep_df=snap, labels=self.labels)
        if quotes.empty:
            return None, "no GTM quotes"
        month_now = f"{t.year:04d}-{t.month:02d}"
        open_months = {tm for tm, s in self.settlements.items()
                       if s.printed_at is None or s.printed_at > t}
        keep = quotes["target_month"].isin(open_months)
        keep &= quotes["con_id"].map(lambda c: self.stops.get(int(c), t + timedelta(days=1)) > t)
        if self.config.skip_running_month:
            keep &= quotes["target_month"] != month_now
        quotes = quotes[keep]
        forecast = self.forecaster.at(t)
        if not forecast.predictive:
            return None, "no forecast"
        ladder = ladder_table(quotes, forecast.predictive, fee_model=self.fee_model)
        if ladder.empty:
            return None, "no priced ladder"
        gated = apply_edge_gate(ladder, fee_model=self.fee_model)
        payload = {
            "ts_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sweep_ts_utc": snap_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "inits": inits_for_priced_targets(forecast.inits, gated),
            "signals": gated.to_dict(orient="records"),
        }
        return (payload, forecast), ""

    def _decide(self, t: pd.Timestamp) -> dict:
        row = {"decision_ts": t, "snapshot_ts": pd.NaT, "skipped": "", "n_ladder": 0,
               "n_tradeable": 0, "n_intents": 0, "n_dropped": 0, "n_placed": 0,
               "n_rejected": 0, "n_repriced": 0, "labels_through": None,
               "model_after_decision": False, "committed_usd": np.nan}
        latest = self.panel.latest_at(t, self.config.max_age)
        if latest is None:
            row["skipped"] = "no fresh snapshot"
            return row
        snap_ts, snap = latest
        row["snapshot_ts"] = snap_ts
        self.venue.set_clock(t, snap_ts)
        built, why = self._payload(t, snap_ts, snap)
        if built is None:
            row["skipped"] = why
            return row
        payload, forecast = built
        row["labels_through"] = forecast.labels_through
        row["model_after_decision"] = forecast.model_after_decision
        row["n_ladder"] = len(payload["signals"])
        row["n_tradeable"] = sum(1 for s in payload["signals"] if s.get("tradeable"))
        cfg = self.config
        intents = intents_from_payload(payload, self.venue, cfg.top, cfg.min_edge,
                                       price_policy=cfg.policy.price_policy,
                                       ticks_below_ask=cfg.policy.ticks_below_ask, now=t)
        row["n_intents"] = len(intents)
        executor = TradingExecutor(venue=self.venue, bankroll=cfg.bankroll,
                                   risk_limits=cfg.risk_limits(), simulate=False,
                                   db_path=self.db, approval_input=lambda _prompt: "y")
        commitments = load_open_commitments(self.db)
        resting = self.venue.get_all_open_orders()
        pre_approved: frozenset[str] = frozenset()
        if cfg.reprice:
            outcome = reprice_resting_orders(executor, self.venue, self.db, intents, resting,
                                             commitments, mode="auto", now=t.to_pydatetime())
            row["n_repriced"] = len(outcome.cancelled_perm_ids)
            self.repriced += row["n_repriced"]
            pre_approved = outcome.replacements
            if outcome.cancelled_perm_ids:
                commitments = load_open_commitments(self.db)
                resting = [o for o in resting
                           if str(o.venue_meta.get("perm_id")) not in outcome.cancelled_perm_ids]
        kept = drop_already_committed(intents, resting, commitments)
        row["n_dropped"] = len(intents) - len(kept)
        executor.risk_manager.seed_exposure(commitments["total_usd"])
        executor.risk_manager.seed_group_exposure(commitments["by_group"])
        report = executor.execute_intents(kept, mode="auto", pre_approved=pre_approved)
        row["n_placed"] = report.trades_executed
        row["n_rejected"] = report.trades_rejected
        row["committed_usd"] = load_open_commitments(self.db)["total_usd"]
        quotes = {i.edge.question_id: i.quote for i in kept}
        for d in report.decisions:
            if not d.executed or d.order is None or not d.order.venue_order_id:
                continue
            q = quotes.get(d.question_id) or {}
            target = d.question_id.split(":")[1]
            self.order_meta[d.order.venue_order_id] = {
                "question_id": d.question_id, "target_month": target,
                "decision_ts": t, "snapshot_ts": snap_ts,
                "lead_months": int((pd.Period(target, freq="M")
                                    - pd.Period(t.tz_convert("UTC").tz_localize(None), freq="M")).n),
                "dec_bid": q.get("bid"), "dec_ask": q.get("ask"),
                "edge_at_limit": abs(d.edge_pct),
            }
        return row

    def _reconcile(self, t: pd.Timestamp) -> None:
        # the guard against a degraded Gateway answer is off: the SimVenue's answer
        # is complete, and a month's orders all expire at once at its stop time
        report = reconcile_paper_book(self.db, self.venue, now=t.to_pydatetime(),
                                      allow_mass_cancel=True)
        if report.aborted:
            self.reconcile_aborts += 1
            self.notes.append(f"{t}: reconcile aborted: {report.notes[-1] if report.notes else ''}")

    def _equity(self, t: pd.Timestamp) -> dict:
        realized = open_cost = open_bid = open_mid = 0.0
        for order in self.venue.orders.values():
            fills = [(ts, q, p) for ts, q, p in order.fills if ts <= t]
            if not fills:
                continue
            qty = sum(q for _, q, _ in fills)
            cost = sum(q * p for _, q, p in fills) + qty * self.config.fee
            tm = self.target_of.get(order.con_id)
            s = self.settlements.get(tm)
            if tm in self.settled and s is not None and s.printed_at is not None and s.printed_at <= t:
                realized += qty * (1.0 if self._wins(order.con_id, s.value) else 0.0) - cost
                continue
            last = self.panel.last_quote(order.con_id, until=t)
            bid, ask = (last[0], last[1]) if last is not None else (0.0, 0.0)
            open_cost += cost
            open_bid += qty * bid
            open_mid += qty * (bid + ask) / 2.0
        return {"ts": t, "realized": realized, "open_cost": open_cost, "open_value_bid": open_bid,
                "open_value_mid": open_mid, "equity_bid": realized + open_bid - open_cost,
                "equity_mid": realized + open_mid - open_cost}

    def run(self, decisions: Sequence[pd.Timestamp],
            on_step: Callable[[dict], None] | None = None) -> ReplayResult:
        """Replay every decision in order; `on_step` hears about each one as it finishes.

        It gets the step number and total, the simulated time, the orders and
        filled orders so far, the equity settled and marked to the bid, the
        decision's skip reason, and the warnings it raised (a reconcile abort,
        the first decision a fixed model may have seen the future of).
        """
        rows, curve = [], []
        flagged_model = False
        n = len(decisions)
        for i, t in enumerate(decisions, start=1):
            t = pd.Timestamp(t)
            aborts = self.reconcile_aborts
            self.venue.advance(t)
            self._settle_due(t)
            self._reconcile(t)
            rows.append(self._decide(t))
            curve.append(self._equity(t))
            if on_step is not None:
                warnings = []
                if self.reconcile_aborts > aborts:
                    warnings.append("reconcile aborted")
                if rows[-1]["model_after_decision"] and not flagged_model:
                    flagged_model = True
                    warnings.append("the fixed model was fitted after this decision")
                on_step({
                    "step": i, "steps": n, "sim_time": t,
                    "orders": len(self.venue.orders),
                    "filled": sum(1 for o in self.venue.orders.values() if o.filled > 0),
                    "pnl_settled": curve[-1]["realized"], "pnl_marked": curve[-1]["equity_bid"],
                    "skipped": rows[-1]["skipped"], "warnings": warnings,
                })
        if not self.panel.empty:
            end = self.panel.times[-1]
            self.venue.advance(end)
            self._settle_due(end)
            self._reconcile(end)
            curve.append(self._equity(end))
        orders = self._orders_frame()
        months = months_frame(orders)
        equity = pd.DataFrame(curve)
        decisions_df = pd.DataFrame(rows)
        metrics = replay_metrics(orders, equity, decisions_df, self.config)
        metrics.update({"reconcile_aborts": self.reconcile_aborts, "repriced": self.repriced,
                        "blocked_by_size": self.venue.blocked_by_size})
        return ReplayResult(self.config, decisions_df, orders, months, equity, metrics, self.notes)

    def _orders_frame(self) -> pd.DataFrame:
        end = self.panel.times[-1] if not self.panel.empty else None
        rows = []
        for oid, order in self.venue.orders.items():
            meta = self.order_meta.get(oid, {})
            c = self.contracts[order.con_id]
            tm = self.target_of.get(order.con_id)
            s = self.settlements.get(tm)
            settled = tm in self.settled
            first_fill = order.fills[0][0] if order.fills else None
            fill_cost = order.cost + order.filled * self.config.fee
            win = settled and self._wins(order.con_id, s.value)
            last = self.panel.last_quote(order.con_id, until=end) if order.filled and not settled else None
            dec_bid, dec_ask = meta.get("dec_bid"), meta.get("dec_ask")
            dec_mid = (dec_bid + dec_ask) / 2 if dec_bid is not None and dec_ask is not None else np.nan
            rows.append({
                "order_id": oid, "perm_id": order.perm_id, "con_id": order.con_id,
                "question_id": meta.get("question_id"), "target_month": tm,
                "strike": c.strike, "leg": "NO" if c.right == "P" else "YES",
                "lead_months": meta.get("lead_months"), "decision_ts": meta.get("decision_ts"),
                "snapshot_ts": meta.get("snapshot_ts"), "limit": order.request.limit_price,
                "qty": order.request.quantity, "dec_bid": dec_bid, "dec_ask": dec_ask,
                "dec_mid": dec_mid, "edge_at_limit": meta.get("edge_at_limit"),
                "status": order.status, "close_reason": order.close_reason,
                "filled": order.filled, "avg_fill": order.avg_price, "first_fill_ts": first_fill,
                "hours_to_fill": ((first_fill - order.placed_at).total_seconds() / 3600
                                  if first_fill is not None else np.nan),
                "fill_cost": fill_cost if order.filled else 0.0,
                "mid_1d": (self.panel.mid_at_or_after(first_fill + timedelta(days=1), order.con_id,
                                                      timedelta(days=2)) if first_fill is not None else np.nan),
                "mid_7d": (self.panel.mid_at_or_after(first_fill + timedelta(days=7), order.con_id,
                                                      timedelta(days=3)) if first_fill is not None else np.nan),
                "settled": settled, "label": s.value if (s is not None and settled) else np.nan,
                "payout": order.filled * (1.0 if win else 0.0) if settled else np.nan,
                "pnl": (order.filled * (1.0 if win else 0.0) - fill_cost) if settled else np.nan,
                "mark_bid": last[0] if last is not None else np.nan,
                "mark_mid": (last[0] + last[1]) / 2 if last is not None else np.nan,
            })
        frame = pd.DataFrame(rows, columns=ORDER_COLUMNS)
        for col in NUMERIC_ORDER_COLUMNS:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype(float)
        frame["settled"] = frame["settled"].astype(bool)
        return frame


# -- results -------------------------------------------------------------------

def months_frame(orders: pd.DataFrame) -> pd.DataFrame:
    """Per target month: orders, fills, cost, and P&L settled or marked to the bid."""
    if orders.empty:
        return pd.DataFrame(columns=["target_month", "orders", "filled_orders", "contracts",
                                     "cost", "settled", "label", "pnl", "unrealized_bid"])
    work = orders.copy()
    work["unrealized_bid"] = np.where(work["settled"], np.nan,
                                      work["filled"] * work["mark_bid"].fillna(0.0) - work["fill_cost"])
    return work.groupby("target_month").agg(
        orders=("order_id", "size"), filled_orders=("filled", lambda s: int((s > 0).sum())),
        contracts=("filled", "sum"), cost=("fill_cost", "sum"), settled=("settled", "all"),
        label=("label", "first"), pnl=("pnl", "sum"), unrealized_bid=("unrealized_bid", "sum"),
    ).reset_index()


def _weighted(values: pd.Series, weights: pd.Series) -> float:
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    ok = np.isfinite(v) & np.isfinite(w) & (w > 0)
    if not ok.any():
        return float("nan")
    return float(np.average(v[ok], weights=w[ok]))


def replay_metrics(orders: pd.DataFrame, equity: pd.DataFrame, decisions: pd.DataFrame,
                   config: ReplayConfig) -> dict:
    """The numbers the grid is compared on."""
    n_dec = len(decisions)
    skipped = decisions["skipped"].value_counts().to_dict() if n_dec else {}
    skipped.pop("", None)
    out = {
        "decisions": n_dec, "skipped": skipped,
        "model_after_decision": int(decisions["model_after_decision"].sum()) if n_dec else 0,
        "orders": int(len(orders)),
    }
    if orders.empty:
        return {**out, "orders_filled": 0, "fill_rate": float("nan"), "contracts": 0.0,
                "capital_deployed": 0.0, "settled_pnl": 0.0, "total_pnl_bid": 0.0,
                "roi_bankroll": 0.0, "max_drawdown": 0.0}
    filled = orders[orders["filled"] > 0]
    on_settled = orders[orders["settled"]]
    settled_filled = filled[filled["settled"]]
    open_filled = filled[~filled["settled"]]
    settled_pnl = float(settled_filled["pnl"].sum())
    settled_cost = float(settled_filled["fill_cost"].sum())
    open_cost = float(open_filled["fill_cost"].sum())
    open_bid = float((open_filled["filled"] * open_filled["mark_bid"].fillna(0.0)).sum())
    open_mid = float((open_filled["filled"] * open_filled["mark_mid"].fillna(0.0)).sum())
    total_bid = settled_pnl + open_bid - open_cost
    curve = equity["equity_bid"].to_numpy() if len(equity) else np.array([0.0])
    drawdown = float(np.max(np.maximum.accumulate(np.r_[0.0, curve]) - np.r_[0.0, curve]))
    by_lead = {}
    for lead, g in filled.groupby("lead_months"):
        pnl = g["pnl"].where(g["settled"], g["filled"] * g["mark_bid"].fillna(0.0) - g["fill_cost"])
        by_lead[str(int(lead))] = float(pnl.sum())
    return {
        **out,
        "orders_filled": int(len(filled)),
        "fill_rate": float(len(filled) / len(orders)),
        "orders_on_settled_months": int(len(on_settled)),
        "fill_rate_settled": (float((on_settled["filled"] > 0).mean()) if len(on_settled) else float("nan")),
        "contracts": float(filled["filled"].sum()),
        "capital_deployed": float(filled["fill_cost"].sum()),
        "median_hours_to_fill": float(filled["hours_to_fill"].median()),
        "paid_over_mid": _weighted(filled["avg_fill"] - filled["dec_mid"], filled["filled"]),
        "markout_1d": _weighted(filled["mid_1d"] - filled["avg_fill"], filled["filled"]),
        "markout_7d": _weighted(filled["mid_7d"] - filled["avg_fill"], filled["filled"]),
        "settled_pnl": settled_pnl, "settled_cost": settled_cost,
        "roi_settled": settled_pnl / settled_cost if settled_cost > 0 else float("nan"),
        "open_cost": open_cost, "open_value_bid": open_bid, "open_value_mid": open_mid,
        "total_pnl_bid": total_bid, "total_pnl_mid": settled_pnl + open_mid - open_cost,
        "roi_bankroll": total_bid / config.bankroll,
        "max_drawdown": drawdown,
        "pnl_by_lead": by_lead,
    }


def d1_verdict(cells: Mapping[str, tuple[ReplayConfig, dict]]) -> list[str]:
    """The plan's D1 rule on the paper-like, one-snapshot-latency cells.

    Keep 'ask' unless 'ask-1' reaches at least 80% of its filled orders with
    P&L (settled plus marked to the bid) at least as high; 'ask-2' faces the
    same test against 'ask-1'.
    """
    base = {cfg.policy.name: m for cfg, m in cells.values()
            if cfg.fill_model == "paper" and cfg.latency == 1 and not cfg.reprice
            and not cfg.skip_running_month}
    if "ask" not in base:
        return ["D1 not evaluated: the grid has no paper-like, latency-1 'ask' cell."]
    lines, choice = [], "ask"
    for tighter, looser in (("ask-1", "ask"), ("ask-2", "ask-1")):
        if looser != choice or tighter not in base or looser not in base:
            break
        a, b = base[tighter], base[looser]
        fills_ok = b.get("orders_filled", 0) == 0 or a.get("orders_filled", 0) >= 0.8 * b["orders_filled"]
        pnl_ok = a.get("total_pnl_bid", 0.0) >= b.get("total_pnl_bid", 0.0)
        lines.append(
            f"{tighter} against {looser}: {a.get('orders_filled', 0)} vs {b.get('orders_filled', 0)} "
            f"filled orders, P&L {a.get('total_pnl_bid', 0.0):+.2f} vs {b.get('total_pnl_bid', 0.0):+.2f}: "
            + ("passes" if fills_ok and pnl_ok else "fails")
        )
        if fills_ok and pnl_ok:
            choice = tighter
        else:
            break
    lines.append(f"D1 choice: {choice}.")
    return lines


__all__ = [
    "CADENCES",
    "Policy",
    "Replay",
    "ReplayConfig",
    "ReplayResult",
    "Settlement",
    "d1_verdict",
    "decision_times",
    "months_frame",
    "replay_metrics",
    "settlements",
    "stop_times",
    "target_months",
]
