"""S3: the GTM paper book traded over past years against synthetic markets."""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timezone

import numpy as np
import pandas as pd
from scipy.stats import norm

from climate.bridge import Bridge
from climate.dataset import build_frame_from_inputs, month_of_year, target_year_frac
from climate.emos import TrendClimatology
from climate.nn.availability import noaa_available_on
from climate.sim.forecast import Forecast, PointInTimeForecaster, print_times
from climate.sim.panel import PANEL_COLUMNS, SWEEP, QuotePanel, merge_sources
from climate.sim.replay import ReplayResult

PROXIES = ("m0", "m1", "m2", "m3", "informed")
PROXY_NAMES = {
    "m0": "trend climatology",
    "m1": "damped persistence",
    "m2": "raw bridged SEAS5",
    "m3": "model shifted",
    "informed": "informed market",
}
MIN_SIGMA = 0.02
FILL_LAG = pd.Timedelta(minutes=20)
STRIKE_OFFSETS = np.round(np.arange(-0.5, 0.51, 0.1), 2)


# -- the model side -------------------------------------------------------------

class StrictForecaster:
    """Point-in-time EMOS with the ERA5-to-NOAA bridge refitted once per calendar year.

    A decision in year y sees a bridge fitted on the labels through December
    of y - 1, as the strict windows of climate/nn/windows.py do, and an EMOS
    refitted on the labels printed by the decision, as the replay does.
    """

    def __init__(self, seas5: pd.DataFrame, era5_K: pd.Series, noaa_C: pd.Series,
                 ledger: pd.DataFrame | None = None, *, trend: bool = True):
        self.seas5, self.era5_K, self.noaa_C = seas5, era5_K, noaa_C
        self.ledger = ledger
        self.trend = trend
        self._by_year: dict[int, PointInTimeForecaster] = {}

    def year(self, year: int) -> PointInTimeForecaster:
        if year not in self._by_year:
            bridge = Bridge().fit(self.era5_K, self.noaa_C, end=f"{year - 1}-12")
            frame = build_frame_from_inputs(self.seas5, self.era5_K, self.noaa_C, bridge=bridge)
            printed = print_times(frame["target_month"].astype(str), self.ledger)
            self._by_year[year] = PointInTimeForecaster(frame, printed, trend=self.trend)
        return self._by_year[year]

    def at(self, t: datetime) -> Forecast:
        return self.year(pd.Timestamp(t).year).at(t)

    def frame_at(self, t: datetime) -> pd.DataFrame:
        """The year's frame restricted to the SEAS5 inits released by `t`."""
        f = self.year(pd.Timestamp(t).year)
        released = (f._released <= pd.Timestamp(t)).to_numpy()
        return f.frame[released]


# -- the market side ---------------------------------------------------------

@dataclass(frozen=True)
class MarketSpec:
    """One synthetic market: a proxy, optionally blended toward the model at weight w."""

    kind: str
    weight: float = 0.0
    sd: float = 0.0  # the informed market's error sd
    bias: float = 0.0  # m3: added to the model's mu
    ratio: float = 1.0  # m3: multiplies the model's sigma

    def __post_init__(self):
        if self.kind not in PROXIES:
            raise ValueError(f"market kind must be one of {PROXIES}")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError("the blend weight must be in [0, 1]")
        if self.kind == "informed" and self.sd <= 0:
            raise ValueError("the informed market needs a positive error sd")

    @property
    def name(self) -> str:
        if self.kind == "informed":
            return f"informed_sd{self.sd:g}"
        return f"{self.kind}_w{self.weight:g}"


def _month_frame(months: Sequence[str]) -> pd.DataFrame:
    months = list(months)
    return pd.DataFrame({"target_month": months,
                         "month_of_year": month_of_year(pd.Series(months)).to_numpy(),
                         "target_year_frac": target_year_frac(pd.Series(months)).to_numpy()})


def _months_between(a: str, b: str) -> int:
    return int((pd.Period(b, freq="M") - pd.Period(a, freq="M")).n)


class MarketModel:
    """mu and sigma per target month that a synthetic market quotes at a decision time."""

    def __init__(self, spec: MarketSpec, labels: pd.Series, printed_at: Mapping[str, datetime],
                 model: StrictForecaster, *, seed: int = 0):
        self.spec = spec
        self.labels = labels.sort_index()
        self.printed_at = {m: pd.Timestamp(t) for m, t in printed_at.items()}
        self.model = model
        self.seed = seed
        self._trend: dict[str, TrendClimatology] = {}
        self._ar: dict[str, tuple[float, float, float]] = {}

    def _printed(self, t: pd.Timestamp) -> pd.Series:
        keep = [m for m in self.labels.index if m in self.printed_at and self.printed_at[m] <= t]
        return self.labels.loc[keep]

    def _trend_fit(self, printed: pd.Series) -> TrendClimatology:
        key = printed.index[-1]
        if key not in self._trend:
            frame = _month_frame(printed.index)
            frame["y_C"] = printed.to_numpy(dtype=float)
            self._trend[key] = TrendClimatology().fit(frame)
        return self._trend[key]

    def _m0(self, printed, targets) -> dict[str, tuple[float, float]]:
        mu, sd = self._trend_fit(printed).predict(_month_frame(targets))
        return {m: (float(a), float(b)) for m, a, b in zip(targets, mu, sd)}

    def _m1(self, printed, targets) -> dict[str, tuple[float, float]]:
        trend = self._trend_fit(printed)
        key = printed.index[-1]
        if key not in self._ar:
            fitted, _ = trend.predict(_month_frame(printed.index))
            r = printed.to_numpy(dtype=float) - fitted
            rho = float(np.clip(np.corrcoef(r[1:], r[:-1])[0, 1], 0.0, 0.99)) if len(r) > 3 else 0.0
            innov = r[1:] - rho * r[:-1]
            self._ar[key] = (rho, float(np.std(innov, ddof=1)), float(r[-1]))
        rho, s_e, last = self._ar[key]
        mu_t, _ = trend.predict(_month_frame(targets))
        out = {}
        for m, base in zip(targets, mu_t):
            h = max(_months_between(key, m), 1)
            var = s_e**2 * ((1 - rho ** (2 * h)) / (1 - rho**2) if rho < 1 else h)
            out[m] = (float(base + rho**h * last), float(np.sqrt(var)))
        return out

    def _m2(self, t, targets, fallback) -> dict[str, tuple[float, float]]:
        frame = self.model.frame_at(t)
        frame = frame[frame["target_month"].isin(targets)].sort_values(["target_month", "lead_month"])
        best = frame.drop_duplicates("target_month", keep="first").set_index("target_month")
        return {m: ((float(best.at[m, "fc_mean_C"]), float(best.at[m, "fc_sd_C"]))
                    if m in best.index else fallback[m]) for m in targets}

    def _informed(self, targets) -> dict[str, tuple[float, float]]:
        out = {}
        for m in targets:
            if m not in self.labels.index:
                continue
            rng = np.random.default_rng([self.seed, zlib.crc32(m.encode())])
            out[m] = (float(self.labels[m] + rng.normal(0.0, self.spec.sd)), self.spec.sd)
        return out

    def at(self, t: datetime, targets: Sequence[str]) -> dict[str, tuple[float, float]]:
        t = pd.Timestamp(t)
        spec = self.spec
        if spec.kind == "informed":
            return self._informed(targets)
        model = self.model.at(t).predictive
        printed = self._printed(t)
        if len(printed) < 3:
            return {}
        if spec.kind == "m3":
            base = {m: (model[m][0] + spec.bias, model[m][1] * spec.ratio)
                    for m in targets if m in model}
        elif spec.kind == "m0":
            base = self._m0(printed, targets)
        elif spec.kind == "m1":
            base = self._m1(printed, targets)
        else:
            base = self._m2(t, targets, self._m0(printed, targets))
        w = spec.weight
        out = {}
        for m, (mu, sd) in base.items():
            if w > 0 and m in model:
                mu, sd = (1 - w) * mu + w * model[m][0], (1 - w) * sd + w * model[m][1]
            out[m] = (mu, max(sd, MIN_SIGMA))
        return out


# -- the synthetic venue -----------------------------------------------------

def hindcast_decisions(first_year: int, last_year: int, *, day: int = 14,
                       at: time = time(14, 0)) -> list[pd.Timestamp]:
    """One decision a month, after the SEAS5 release (the 13th) and the NOAA print (the 12th)."""
    return [pd.Timestamp(datetime(y, m, day, at.hour, at.minute, tzinfo=timezone.utc))
            for y in range(first_year, last_year + 1) for m in range(1, 13)]


def expiry_for(target: str) -> str:
    """The synthetic contract's expiry: the 12th of the month after its target, YYYYMMDD."""
    nxt = pd.Period(target, freq="M") + 1
    return f"{nxt.year:04d}{nxt.month:02d}12"


@dataclass
class SyntheticVenue:
    """The quotes a synthetic market shows over the hindcast, as a replay panel."""

    panel: QuotePanel
    strikes: dict[str, np.ndarray]
    quoted_months: list[str]


def _quote(prob: np.ndarray, ceiling: float, half_spread: float) -> tuple[np.ndarray, np.ndarray]:
    mid = ceiling * prob
    bid = np.clip(np.floor((mid - half_spread) * 100 + 1e-9) / 100, 0.01, 0.98)
    ask = np.clip(np.ceil((mid + half_spread) * 100 - 1e-9) / 100, 0.02, 0.99)
    ask = np.maximum(ask, bid + 0.01)
    return np.round(bid, 2), np.round(ask, 2)


def build_venue(market: MarketModel, decisions: Sequence[pd.Timestamp], *, last_month: str,
                months_ahead: int = 4, half_spread: float = 0.015, ceiling: float = 1.0,
                strike_market: MarketModel | None = None) -> SyntheticVenue:
    """Quotes at every decision (and once more FILL_LAG later) for the months open then.

    A target month is quoted from `months_ahead` months before it through its
    own month, never past `last_month`. Its strikes are fixed when it is
    first quoted: eleven, 0.1 degC apart, around the trend climatology of
    that moment (`strike_market`, default an m0 market), as a venue lists a
    ladder once. The last open months are quoted once more after their print,
    so the replay can settle them.
    """
    strike_market = strike_market or MarketModel(MarketSpec("m0"), market.labels,
                                                 market.printed_at, market.model)
    rows, strikes, ids = [], {}, {}
    next_id = 1_000_001

    def con(target, k, right):
        nonlocal next_id
        key = (target, round(float(k), 2), right)
        if key not in ids:
            ids[key] = next_id
            next_id += 1
        return ids[key]

    last_quotes: dict[str, tuple] = {}
    for t in decisions:
        now = pd.Period(t.tz_convert("UTC").tz_localize(None), freq="M")
        targets = [str(now + h) for h in range(months_ahead + 1) if str(now + h) <= last_month]
        if not targets:
            continue
        new = [m for m in targets if m not in strikes]
        if new:
            centres = strike_market.at(t, new)
            for m in new:
                if m in centres:
                    strikes[m] = np.round(round(centres[m][0], 1) + STRIKE_OFFSETS, 2)
        dists = market.at(t, [m for m in targets if m in strikes])
        for m, (mu, sd) in dists.items():
            k = strikes[m]
            bid, ask = _quote(norm.sf((k - mu) / sd), ceiling, half_spread)
            last_quotes[m] = (k, bid, ask)
            for ts in (t, t + FILL_LAG):
                rows.extend(_rows(ts, m, k, bid, ask, con))
    if last_quotes:
        settle_at = max(pd.Timestamp(noaa_available_on(m)) for m in last_quotes) + pd.Timedelta(days=1)
        for m, (k, bid, ask) in last_quotes.items():
            rows.extend(_rows(settle_at, m, k, bid, ask, con))
    frame = pd.DataFrame(rows, columns=PANEL_COLUMNS)
    return SyntheticVenue(QuotePanel(merge_sources(frame, pd.DataFrame(columns=PANEL_COLUMNS))),
                          strikes, sorted(strikes))


def _rows(ts, target, strikes, bid, ask, con) -> list[dict]:
    expiry = expiry_for(target)
    out = []
    for k, b, a in zip(strikes, bid, ask):
        for right, bb, aa in (("C", b, a), ("P", round(1 - a, 2), round(1 - b, 2))):
            out.append({"ts": ts, "source": SWEEP, "con_id": con(target, k, right), "symbol": "GTM",
                        "expiry": expiry, "strike": float(k), "right": right, "bid": float(bb),
                        "ask": float(aa), "bid_size": 50.0, "ask_size": 50.0})
    return out


def settlement_values(labels: pd.Series, months: Iterable[str], *, noise: float = 0.0,
                      seed: int = 0) -> dict[str, tuple[float, str]]:
    """month -> (settling value, vintage): the label, plus N(0, noise) for first-print error."""
    out = {}
    for m in months:
        if m not in labels.index or not np.isfinite(labels[m]):
            continue
        value = float(labels[m])
        if noise > 0:
            value += float(np.random.default_rng([seed, 7, zlib.crc32(m.encode())]).normal(0, noise))
        out[m] = (round(value, 2), "current_vintage" + (f"+N(0,{noise:g})" if noise > 0 else ""))
    return out


# -- results -----------------------------------------------------------------

def yearly_table(result: ReplayResult, years: Sequence[int]) -> pd.DataFrame:
    """Per target year: settled P&L, orders filled, contracts, cost and the share that paid."""
    orders = result.orders
    filled = orders[(orders["filled"] > 0) & orders["settled"]].copy() if len(orders) else orders
    if len(filled):
        filled["year"] = filled["target_month"].str[:4].astype(int)
        filled["won"] = filled["payout"] > 0
    rows = []
    for y in years:
        g = filled[filled["year"] == y] if len(filled) else filled
        rows.append({"year": y, "pnl": float(g["pnl"].sum()) if len(g) else 0.0,
                     "orders_filled": int(len(g)),
                     "contracts": float(g["filled"].sum()) if len(g) else 0.0,
                     "cost": float(g["fill_cost"].sum()) if len(g) else 0.0,
                     "hit_rate": float(g["won"].mean()) if len(g) else float("nan")})
    return pd.DataFrame(rows)


def hindcast_metrics(yearly: pd.DataFrame, result: ReplayResult, split_year: int = 2013) -> dict:
    """Totals, the spread of yearly P&L and its t statistic, the two periods, the drawdown."""
    pnl = yearly["pnl"].to_numpy(dtype=float)
    n = len(pnl)
    sd = float(np.std(pnl, ddof=1)) if n > 1 else float("nan")
    mean = float(np.mean(pnl)) if n else float("nan")
    curve = np.cumsum(pnl)
    filled = int(yearly["orders_filled"].sum())
    cost = float(yearly["cost"].sum())
    wins = result.orders[(result.orders["filled"] > 0) & result.orders["settled"]] \
        if len(result.orders) else result.orders
    return {
        "total_pnl": float(pnl.sum()), "mean_year": mean, "sd_year": sd,
        "t_stat": mean / sd * np.sqrt(n) if n > 1 and sd > 0 else float("nan"),
        "share_years_up": float(np.mean(pnl > 0)) if n else float("nan"),
        "orders_filled": filled, "contracts": float(yearly["contracts"].sum()),
        "capital_deployed": cost, "roi_deployed": float(pnl.sum()) / cost if cost > 0 else float("nan"),
        "hit_rate": float((wins["payout"] > 0).mean()) if len(wins) else float("nan"),
        "pnl_before": float(yearly.loc[yearly["year"] < split_year, "pnl"].sum()),
        "pnl_from": float(yearly.loc[yearly["year"] >= split_year, "pnl"].sum()),
        "max_drawdown": float(np.max(np.maximum.accumulate(np.r_[0.0, curve]) - np.r_[0.0, curve])),
        "pnl_by_lead": result.metrics.get("pnl_by_lead", {}),
        "reconcile_aborts": result.metrics.get("reconcile_aborts", 0),
    }


def breakeven(points: Sequence[tuple[float, float]], *, rising: bool = False) -> float | None:
    """Where P&L crosses zero along a sorted grid, linearly interpolated; None without a crossing.

    Falling (blend weights): the first x at which P&L is at or below zero
    after a positive one. Rising (informed-market error sd): the first x at
    which it turns positive after a value at or below zero.
    """
    pts = sorted(points)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        crossed = (y0 > 0 >= y1) if not rising else (y0 <= 0 < y1)
        if crossed:
            return float(x0 + (x1 - x0) * (y0 / (y0 - y1))) if y0 != y1 else float(x1)
    return None


__all__ = [
    "PROXIES",
    "PROXY_NAMES",
    "MarketModel",
    "MarketSpec",
    "StrictForecaster",
    "SyntheticVenue",
    "breakeven",
    "build_venue",
    "expiry_for",
    "hindcast_decisions",
    "hindcast_metrics",
    "settlement_values",
    "yearly_table",
]
