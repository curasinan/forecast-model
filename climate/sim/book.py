"""P&L of a GTM book for one print, exact under a Gaussian outcome, across scenarios."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

from climate.sim.implied import ImpliedFit

PRINT_RESOLUTION = 0.01  # NOAA prints to 0.01 degC; YES at k pays iff the print > k


@dataclass(frozen=True)
class Position:
    """`qty` contracts of one leg bought at `price` (a YES pays 1 iff the print > strike)."""

    strike: float
    leg: str
    qty: float
    price: float

    def __post_init__(self):
        if self.leg not in ("YES", "NO"):
            raise ValueError(f"leg must be YES or NO, got {self.leg!r}")
        if self.qty <= 0 or not (0.0 < self.price < 1.0):
            raise ValueError(f"bad position {self!r}")

    @classmethod
    def parse(cls, text: str) -> Position:
        """'STRIKE:LEG:QTY:PRICE', e.g. '1.3:YES:22:0.44'."""
        parts = text.split(":")
        if len(parts) != 4:
            raise ValueError(f"position {text!r} is not STRIKE:LEG:QTY:PRICE")
        strike, leg, qty, price = parts
        return cls(float(strike), leg.upper(), float(qty), float(price))


@dataclass(frozen=True)
class Scenario:
    """One belief about the print: N(mu, sigma), with where it came from."""

    name: str
    mu: float
    sigma: float
    source: str


def book_cost(positions: Sequence[Position], fee_per_contract: float) -> float:
    """What the book paid: prices plus the per-contract commission."""
    return float(sum(p.qty * (p.price + fee_per_contract) for p in positions))


def settlement_value(positions: Sequence[Position], printed: float) -> float:
    """What the book is paid when NOAA prints `printed` (already rounded to 0.01)."""
    total = 0.0
    for p in positions:
        wins = printed > p.strike + 1e-12 if p.leg == "YES" else printed <= p.strike + 1e-12
        total += p.qty if wins else 0.0
    return total


def outcome_table(positions: Sequence[Position], mu: float, sigma: float, *,
                  fee_per_contract: float = 0.01,
                  resolution: float = PRINT_RESOLUTION) -> pd.DataFrame:
    """Every distinct settlement of the book with its probability under N(mu, sigma).

    The true anomaly y prints as y rounded to `resolution`, so a YES at strike
    k pays iff y >= k + resolution / 2. Between two neighbouring strikes the
    book's value is constant, so the table is exact.
    """
    if sigma <= 0 or not np.isfinite(sigma) or not np.isfinite(mu):
        raise ValueError("mu must be finite and sigma positive")
    edges = sorted({p.strike + resolution / 2.0 for p in positions})
    bounds = [-np.inf, *edges, np.inf]
    cost = book_cost(positions, fee_per_contract)
    rows = []
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        prob = float(norm.cdf((hi - mu) / sigma) - norm.cdf((lo - mu) / sigma))
        printed = (lo + resolution / 2.0) if np.isfinite(lo) else (hi - resolution)
        value = settlement_value(positions, round(printed, 6))
        rows.append({"print_from": lo, "print_to": hi, "prob": prob,
                     "value": value, "pnl": value - cost})
    return pd.DataFrame(rows)


def summarize(table: pd.DataFrame, cost: float) -> dict:
    """Expected P&L and ROI, chance of a loss and of losing everything, and the quantiles."""
    prob = table["prob"].to_numpy()
    pnl = table["pnl"].to_numpy()
    mean = float(np.sum(prob * pnl))
    order = np.argsort(pnl)
    cum = np.cumsum(prob[order])

    def quantile(q: float) -> float:
        return float(pnl[order][min(int(np.searchsorted(cum, q)), len(cum) - 1)])

    return {
        "cost": cost,
        "expected_pnl": mean,
        "expected_roi": mean / cost if cost > 0 else float("nan"),
        "sd_pnl": float(np.sqrt(np.sum(prob * (pnl - mean) ** 2))),
        "p_loss": float(prob[pnl < -1e-9].sum()),
        "p_total_loss": float(prob[pnl <= -cost + 1e-9].sum()),
        "pnl_q05": quantile(0.05),
        "pnl_median": quantile(0.5),
        "worst": float(pnl.min()),
        "best": float(pnl.max()),
    }


def breakeven_mu(positions: Sequence[Position], sigma: float, *,
                 fee_per_contract: float = 0.01,
                 resolution: float = PRINT_RESOLUTION) -> float | None:
    """The mu at which the book's expected P&L is zero for this sigma; None without a crossing."""
    def expected(mu: float) -> float:
        table = outcome_table(positions, mu, sigma, fee_per_contract=fee_per_contract,
                              resolution=resolution)
        return float(np.sum(table["prob"] * table["pnl"]))

    strikes = [p.strike for p in positions]
    lo, hi = min(strikes) - 1.0 - 6 * sigma, max(strikes) + 1.0 + 6 * sigma
    f_lo, f_hi = expected(lo), expected(hi)
    if np.sign(f_lo) == np.sign(f_hi):
        return None
    return float(brentq(expected, lo, hi, xtol=1e-6))


def build_scenarios(models: Sequence[Scenario], market: ImpliedFit | None, *,
                    blends: Iterable[float] = (0.25, 0.5, 0.75),
                    mu_shifts: Iterable[float] = (-0.10, -0.05, 0.05, 0.10),
                    sigma_scales: Iterable[float] = (1.25, 1.5)) -> list[Scenario]:
    """The models, the market, blends of the first model with the market, and model stresses.

    A blend at weight w takes mu and sigma as (1 - w) * market + w * model.
    """
    out = list(models)
    if not models:
        return out
    base = models[0]
    if market is not None and market.identified:
        out.append(Scenario("market", market.mu, market.sigma, "market-implied, ceiling removed"))
        for w in blends:
            out.append(Scenario(
                f"blend_w{w:g}", (1 - w) * market.mu + w * base.mu,
                (1 - w) * market.sigma + w * base.sigma,
                f"{w:g} x {base.name} + {1 - w:g} x market",
            ))
    for d in mu_shifts:
        out.append(Scenario(f"{base.name}_mu{d:+g}", base.mu + d, base.sigma, f"{base.name}, mu {d:+g}"))
    for s in sigma_scales:
        out.append(Scenario(f"{base.name}_sigma_x{s:g}", base.mu, base.sigma * s,
                            f"{base.name}, sigma x {s:g}"))
    return out


def scenario_table(books: dict[str, Sequence[Position]], scenarios: Sequence[Scenario], *,
                   fee_per_contract: float = 0.01,
                   resolution: float = PRINT_RESOLUTION) -> pd.DataFrame:
    """One row per (book, scenario) with the summary of its outcome table."""
    rows = []
    for book_name, positions in books.items():
        cost = book_cost(positions, fee_per_contract)
        for sc in scenarios:
            table = outcome_table(positions, sc.mu, sc.sigma, fee_per_contract=fee_per_contract,
                                  resolution=resolution)
            rows.append({"book": book_name, "scenario": sc.name, "source": sc.source,
                         "mu": sc.mu, "sigma": sc.sigma, **summarize(table, cost)})
    return pd.DataFrame(rows)


def mark_to_market(positions: Sequence[Position], quotes: pd.DataFrame) -> dict:
    """The book valued at the ladder's mids and bids (`quotes`: strike, bid, ask), YES legs only."""
    by_strike = {round(float(r.strike), 6): r for r in quotes.itertuples(index=False)}
    at_mid = at_bid = 0.0
    missing = []
    for p in positions:
        q = by_strike.get(round(p.strike, 6))
        if p.leg != "YES" or q is None or not np.isfinite(q.bid) or not np.isfinite(q.ask):
            missing.append(p.strike)
            continue
        at_mid += p.qty * (q.bid + q.ask) / 2.0
        at_bid += p.qty * q.bid
    return {"value_at_mid": at_mid, "value_at_bid": at_bid, "unpriced_strikes": missing}


__all__ = [
    "PRINT_RESOLUTION",
    "Position",
    "Scenario",
    "book_cost",
    "breakeven_mu",
    "build_scenarios",
    "mark_to_market",
    "outcome_table",
    "scenario_table",
    "settlement_value",
    "summarize",
]
