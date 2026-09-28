"""Go-live and edge gates re-scoped for GTM (~12 independent resolutions/year).

Weather's pre-registered bar (`weather/paper_criteria.py`) asked for 200
resolved buckets in 90 paper days. That cadence is a daily city-temperature
ladder. GTM settles once a month; 200 independent samples would take ~17
years. This module replaces that bar with two gates:

1. **Hindcast skill** (sample-rich, available now): expanding-window CRPS
   of the GTM EMOS vs monthly trend climatology, plus interval coverage.
   This is the model-quality gate.
2. **Live paper** (slow): calendar time + resolved *target months*, not
   buckets. Default 365 paper days and 12 resolved months.

Edge is re-derived from the verified ForecastEx friction, not copied from
the Polymarket weather defaults (min_edge 0.05, band 0.02-0.98):

    min_edge = commission_per_contract + half_spread
             = 0.01 + max(observed_spread, min_tick) / 2

With a 1-tick book that is 0.015; we round the *floor* up to 0.02 so a
trade must clear the commission plus a full tick of half-spread slack.
Deep ITM/OTM quotes (price < 0.05 or > 0.95) are excluded: a 1-cent tick
is a huge relative move there, and the audit flagged strike-ladder extremes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping

import numpy as np
import pandas as pd

from src.domain.fees import FeeModel, FlatCommissionFeeModel
from src.math.expected_value import expected_value
from src.math.kelly import half_kelly

# Verified 2026-08-16 (docs/venue_semantics.md). Research default only.
DEFAULT_COMMISSION = 0.01
DEFAULT_MIN_TICK = 0.01
# Floor: commission + 1 tick. Observed half-spread is added on top when known.
DEFAULT_MIN_EDGE = DEFAULT_COMMISSION + DEFAULT_MIN_TICK  # 0.02
DEFAULT_MIN_PRICE = 0.05
DEFAULT_MAX_PRICE = 0.95
MAX_SANE_SPREAD = 0.10


@dataclass(frozen=True)
class ClimateGoLiveCriteria:
    """Anti-optional-stopping bar for the climate lane (GTM first)."""

    min_skill_vs_trend: float = 0.0
    min_leads_beating_trend: int = 3  # any 3 leads must clear the skill floor
    min_cover50: float = 0.35
    max_cover50: float = 0.65
    min_cover90: float = 0.75
    max_cover90: float = 0.98
    min_paper_days: int = 365
    min_resolved_months: int = 12
    min_oos_rows_per_lead: int = 24


@dataclass
class GoLiveVerdict:
    passed: bool
    reasons: list[str]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": self.reasons, "metrics": self.metrics}


def evaluate_hindcast_go_live(
    summary: pd.DataFrame,
    *,
    criteria: ClimateGoLiveCriteria | None = None,
    paper_days: int = 0,
    resolved_months: int = 0,
    require_live_paper: bool = False,
) -> GoLiveVerdict:
    """Score the per-lead expanding-window summary against the climate bar.

    ``require_live_paper`` is False until a paper book is actually running:
    hindcast skill can pass on its own. Flip it True to enforce the slow
    live-month gate as well (Step 6 paper loop).
    """
    criteria = criteria or ClimateGoLiveCriteria()
    reasons: list[str] = []
    metrics: dict[str, Any] = {
        "criteria": asdict(criteria),
        "paper_days": paper_days,
        "resolved_months": resolved_months,
        "leads": [],
    }
    if summary is None or summary.empty:
        reasons.append("no expanding-window summary (model not scored)")
        return GoLiveVerdict(False, reasons, metrics)

    beating = 0
    for _, row in summary.sort_values("lead_month").iterrows():
        lead = int(row["lead_month"])
        skill = float(row["skill_vs_trend"]) if np.isfinite(row["skill_vs_trend"]) else float("nan")
        n = int(row["n"]) if np.isfinite(row["n"]) else 0
        c50 = float(row["cover50_emos"]) if np.isfinite(row["cover50_emos"]) else float("nan")
        c90 = float(row["cover90_emos"]) if np.isfinite(row["cover90_emos"]) else float("nan")
        metrics["leads"].append(
            {"lead_month": lead, "n": n, "skill_vs_trend": skill, "cover50": c50, "cover90": c90}
        )
        if n < criteria.min_oos_rows_per_lead:
            reasons.append(f"lead {lead}: n={n} < {criteria.min_oos_rows_per_lead}")
        if not np.isfinite(skill) or skill < criteria.min_skill_vs_trend:
            reasons.append(
                f"lead {lead}: skill_vs_trend {skill if np.isfinite(skill) else 'nan'} "
                f"< {criteria.min_skill_vs_trend}"
            )
        else:
            beating += 1
        if not np.isfinite(c50):
            # M2b/L4: a NaN coverage row used to sail through silently
            # (neither branch of the old isfinite-guarded check fired for
            # it) -- strengthen to an explicit refusal reason instead.
            reasons.append(f"lead {lead}: cover50 is NaN (coverage could not be computed)")
        elif not (criteria.min_cover50 <= c50 <= criteria.max_cover50):
            reasons.append(
                f"lead {lead}: cover50 {c50:.3f} outside "
                f"[{criteria.min_cover50}, {criteria.max_cover50}]"
            )
        if not np.isfinite(c90):
            reasons.append(f"lead {lead}: cover90 is NaN (coverage could not be computed)")
        elif not (criteria.min_cover90 <= c90 <= criteria.max_cover90):
            reasons.append(
                f"lead {lead}: cover90 {c90:.3f} outside "
                f"[{criteria.min_cover90}, {criteria.max_cover90}]"
            )

    if beating < criteria.min_leads_beating_trend:
        reasons.append(
            f"{beating} leads beat trend climatology < min {criteria.min_leads_beating_trend}"
        )

    if require_live_paper:
        if paper_days < criteria.min_paper_days:
            reasons.append(f"paper_days {paper_days} < {criteria.min_paper_days}")
        if resolved_months < criteria.min_resolved_months:
            reasons.append(
                f"resolved_months {resolved_months} < {criteria.min_resolved_months}"
            )

    return GoLiveVerdict(passed=len(reasons) == 0, reasons=reasons, metrics=metrics)


@dataclass(frozen=True)
class ClimateEdgeGate:
    """Fee-aware tradeability filter for one GTM YES contract."""

    commission_per_contract: float = DEFAULT_COMMISSION
    min_tick: float = DEFAULT_MIN_TICK
    min_edge: float = DEFAULT_MIN_EDGE
    min_price: float = DEFAULT_MIN_PRICE
    max_price: float = DEFAULT_MAX_PRICE
    max_spread: float = MAX_SANE_SPREAD


@dataclass
class ClimateEdgeSignal:
    p_model: float
    yes_mid: float
    spread: float
    fee: float
    edge: float
    ev_after_fee: float
    half_kelly: float
    tradeable: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "p_model": self.p_model,
            "yes_mid": self.yes_mid,
            "spread": self.spread,
            "fee": self.fee,
            "edge": self.edge,
            "ev_after_fee": self.ev_after_fee,
            "half_kelly": self.half_kelly,
            "tradeable": self.tradeable,
            "reason": self.reason,
        }


def min_edge_from_spread(
    spread: float,
    *,
    commission: float = DEFAULT_COMMISSION,
    min_tick: float = DEFAULT_MIN_TICK,
) -> float:
    """commission + half of max(spread, min_tick); never below DEFAULT_MIN_EDGE."""
    half = max(float(spread) if np.isfinite(spread) else min_tick, min_tick) / 2.0
    return max(DEFAULT_MIN_EDGE, commission + half)


def evaluate_climate_edge(
    p_model: float,
    yes_mid: float,
    spread: float,
    *,
    gate: ClimateEdgeGate | None = None,
    fee_model: FeeModel | None = None,
    suspect_quote: bool = False,
) -> ClimateEdgeSignal:
    """Buy-YES view: edge = p_model - mid, EV after the flat commission.

    Uses mid (not ask) because ForecastEx delayed quotes are 20-min snapshots
    and the recorder does not guarantee the ask is actionable. Suspect or
    missing books are never tradeable.
    """
    gate = gate or ClimateEdgeGate()
    fee_model = fee_model or FlatCommissionFeeModel(gate.commission_per_contract)
    fee = float(fee_model.execution_cost(yes_mid if np.isfinite(yes_mid) else 0.0, 1))
    reason = ""
    tradeable = True
    if suspect_quote or not np.isfinite(yes_mid) or not np.isfinite(spread):
        tradeable, reason = False, "suspect_quote"
    elif not (0.0 <= p_model <= 1.0):
        tradeable, reason = False, "invalid_model_p"
    elif yes_mid <= gate.min_price or yes_mid >= gate.max_price:
        tradeable, reason = False, "price_extreme"
    elif spread < 0 or spread > gate.max_spread:
        tradeable, reason = False, "spread_insane"
    edge = float(p_model - yes_mid) if np.isfinite(yes_mid) else float("nan")
    ev = float("nan")
    hk = 0.0
    if np.isfinite(yes_mid) and np.isfinite(p_model):
        ev = float(
            expected_value(p_model, yes_mid, fee_model=fee_model, holding_days=0.0)
        )
        needed = min_edge_from_spread(
            spread, commission=gate.commission_per_contract, min_tick=gate.min_tick
        )
        needed = max(needed, gate.min_edge)
        if tradeable and edge < needed:
            tradeable, reason = False, "edge_below_threshold"
        if tradeable:
            hk = float(half_kelly(p_model, yes_mid, fee_model=fee_model))
    return ClimateEdgeSignal(
        p_model=float(p_model),
        yes_mid=float(yes_mid) if np.isfinite(yes_mid) else float("nan"),
        spread=float(spread) if np.isfinite(spread) else float("nan"),
        fee=fee,
        edge=edge,
        ev_after_fee=ev,
        half_kelly=hk,
        tradeable=tradeable,
        reason=reason,
    )


def apply_edge_gate(
    ladder: pd.DataFrame,
    *,
    gate: ClimateEdgeGate | None = None,
    fee_model: FeeModel | None = None,
) -> pd.DataFrame:
    """Add tradeable / reason / half_kelly columns to a ``ladder_table`` frame."""
    gate = gate or ClimateEdgeGate()
    recs = []
    for _, row in ladder.iterrows():
        sig = evaluate_climate_edge(
            float(row["p_model"]),
            float(row["yes_mid"]) if np.isfinite(row.get("yes_mid", np.nan)) else float("nan"),
            float(row["spread"]) if np.isfinite(row.get("spread", np.nan)) else float("nan"),
            gate=gate,
            fee_model=fee_model,
            suspect_quote=bool(row.get("suspect_quote", False)),
        )
        recs.append(sig.to_dict())
    extra = pd.DataFrame(recs)
    out = ladder.copy().reset_index(drop=True)
    for col in extra.columns:
        out[col] = extra[col].to_numpy()
    return out


@dataclass(frozen=True)
class StalenessGates:
    """Operator-approved refusal thresholds for pricing inputs (audit H2).

    The weekly failure mode this guards against: a logged-out IBKR Gateway
    leaves days-old sweep parquets and signal files sitting on disk, and
    without an age check the paper chain happily prices -- and would
    place GTC orders -- off them. ``require_fresh_init`` additionally
    catches a SEAS5 fetch that was simply never re-run this month: the
    freshest AVAILABLE init can be perfectly parseable and still be a
    month stale.
    """

    max_sweep_age_min: float = 40.0
    max_signal_age_min: float = 90.0
    require_fresh_init: bool = True


def expected_latest_init(today: date) -> str:
    """'YYYY-MM' of the SEAS5 init an operator should expect to be freshest.

    SEAS5 arrives ~the 5th-6th of each month (manual fetch); on/after the
    7th this month's init is expected to exist, before that the previous
    month's is still the freshest one that could possibly have landed.
    """
    year, month = today.year, today.month
    if today.day < 7:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return f"{year:04d}-{month:02d}"


def check_staleness(
    *,
    sweep_ts: datetime | None,
    signal_ts: datetime | None,
    inits_by_target: Mapping[str, str] | None,
    now: datetime,
    gates: StalenessGates | None = None,
) -> list[str]:
    """Human-readable refusal reasons for stale pricing inputs; empty == fresh.

    Each of ``sweep_ts``, ``signal_ts``, ``inits_by_target`` may be None,
    which skips ONLY that component's check -- reserved for data that is
    absent BY DESIGN (e.g. a caller that has no signal-file timestamp to
    offer at all). A caller that HAS the data must never pass None for it:
    that would silently defeat the gate it is supposed to pass through.
    Both paper entrypoints (``scripts/run_gtm_paper.py``,
    ``scripts/run_ibkr_paper.py``) always have every input they call this
    with, so they never take that skip path in practice.
    """
    gates = gates or StalenessGates()
    reasons: list[str] = []
    if sweep_ts is not None:
        age_min = (now - sweep_ts).total_seconds() / 60.0
        if age_min > gates.max_sweep_age_min:
            reasons.append(
                f"sweep is {age_min:.1f} min old, max {gates.max_sweep_age_min:.0f} min "
                "(recorder sweep may be stale -- check the Gateway session)"
            )
    if signal_ts is not None:
        age_min = (now - signal_ts).total_seconds() / 60.0
        if age_min > gates.max_signal_age_min:
            reasons.append(
                f"signal file is {age_min:.1f} min old, max {gates.max_signal_age_min:.0f} "
                "min (re-run scripts/run_gtm_paper.py)"
            )
    if gates.require_fresh_init and inits_by_target is not None:
        expected = expected_latest_init(now.date())
        for target in sorted(inits_by_target):
            init = inits_by_target[target]
            if init != expected:
                reasons.append(
                    f"target {target}: SEAS5 init {init} is not the expected latest "
                    f"{expected} (fetch this month's SEAS5 init)"
                )
    return reasons


__all__ = [
    "DEFAULT_COMMISSION",
    "DEFAULT_MIN_EDGE",
    "DEFAULT_MIN_PRICE",
    "DEFAULT_MAX_PRICE",
    "ClimateGoLiveCriteria",
    "GoLiveVerdict",
    "evaluate_hindcast_go_live",
    "ClimateEdgeGate",
    "ClimateEdgeSignal",
    "min_edge_from_spread",
    "evaluate_climate_edge",
    "apply_edge_gate",
    "StalenessGates",
    "expected_latest_init",
    "check_staleness",
]
