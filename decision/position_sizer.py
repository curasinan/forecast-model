"""Quarter-Kelly position sizing from the validated model's hit-rate and win/loss ratio."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import NamedTuple

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "polymarket.db"
DEFAULT_METRICS_PATH = REPO_ROOT / "model" / "results" / "metrics_6h_v2.json"

DEFAULT_KELLY_MULTIPLIER = 0.25            # quarter-Kelly
DEFAULT_MAX_SINGLE_POSITION_PCT = 0.05     # 5% of bankroll
DEFAULT_MIN_POSITION_DOLLARS = 5.0         # below this → not worth the overhead
DEFAULT_B_FLOOR = 0.1                      # b < this would explode the formula
DEFAULT_B_CEILING = 10.0                   # b > this is suspicious; cap as defensive fallback

log = logging.getLogger("decision.position_sizer")


# Public API


class KellyInputs(NamedTuple):
    p: float        # probability of winning (per-trade hit rate)
    b: float        # avg_win / avg_loss
    avg_win: float
    avg_loss: float


def size_positions(
    edges_df: pd.DataFrame,
    bankroll: float,
    db_path: Path | str = DEFAULT_DB_PATH,
    metrics_path: Path | str = DEFAULT_METRICS_PATH,
    kelly_multiplier: float = DEFAULT_KELLY_MULTIPLIER,
    max_single_position_pct: float = DEFAULT_MAX_SINGLE_POSITION_PCT,
    min_position_dollars: float = DEFAULT_MIN_POSITION_DOLLARS,
) -> pd.DataFrame:
    """Add ``kelly_fraction`` + ``position_dollars`` columns to ``edges_df``."""
    result = edges_df.copy()
    result["kelly_fraction"] = 0.0
    result["position_dollars"] = 0.0

    if result.empty:
        return result

    if "tradeable" not in result.columns:
        raise RuntimeError(
            "edges_df must contain a 'tradeable' boolean column (compute_edges output)"
        )

    # Bankroll defenses: 0 / negative bankrolls mean no positions at all.
    if not bankroll or bankroll <= 0:
        log.warning(
            "size_positions called with bankroll=%s — all positions sized to 0", bankroll,
        )
        result["tradeable"] = False
        return result

    kelly = _load_kelly_inputs(metrics_path)
    fraction = _kelly_fraction(kelly.p, kelly.b)
    quarter = fraction * kelly_multiplier

    log.info(
        "kelly inputs from %s: p=%.4f avg_win=%.4f avg_loss=%.4f b=%.3f "
        "kelly_fraction=%.4f quarter=%.4f",
        Path(metrics_path).name, kelly.p, kelly.avg_win, kelly.avg_loss, kelly.b,
        fraction, quarter,
    )

    tradeable_mask = result["tradeable"].astype(bool)
    raw = quarter * bankroll
    capped = min(raw, max_single_position_pct * bankroll)
    result.loc[tradeable_mask, "kelly_fraction"] = float(fraction)
    result.loc[tradeable_mask, "position_dollars"] = float(capped)

    # Apply the $5 floor: tiny positions become no-trades.
    below_floor = (result["position_dollars"] < min_position_dollars) & tradeable_mask
    n_below_floor = int(below_floor.sum())
    if n_below_floor:
        log.info(
            "below-$%g floor: %d position(s) flipped to not-tradeable",
            min_position_dollars, n_below_floor,
        )
    result.loc[below_floor, "tradeable"] = False
    result.loc[below_floor, "position_dollars"] = 0.0

    total_to_deploy = float(result.loc[result["tradeable"], "position_dollars"].sum())
    n_tradeable = int(result["tradeable"].sum())
    log.info(
        "sizer wants to deploy $%.2f across %d signals (kelly=%.4f, capped at %.1f%% per position)",
        total_to_deploy, n_tradeable, fraction, max_single_position_pct * 100,
    )
    return result


# Internals


def _kelly_fraction(p: float, b: float) -> float:
    """Binary Kelly: f* = max(0, p - (1-p)/b). Clipped at 0 for negative edge."""
    if b <= 0:
        return 0.0
    raw = p - (1.0 - p) / b
    return max(0.0, raw)


def _load_kelly_inputs(metrics_path: Path | str) -> KellyInputs:
    """Read p, avg_win, avg_loss, b from the model's metrics_6h_v2.json (FULL sim)."""
    p_path = Path(metrics_path)
    if not p_path.exists():
        raise RuntimeError(
            f"metrics file not found at {p_path} — train the 6h model first via "
            f"`python -m model.train --horizon 6 --features stable --threshold 0.005 "
            f"--metrics-suffix _6h_v2`"
        )

    try:
        with p_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"metrics file at {p_path} is not valid JSON: {exc}") from exc

    sim = (data.get("profit_simulation") or {}).get("full")
    if not isinstance(sim, dict):
        raise RuntimeError(
            f"metrics_6h_v2.json at {p_path} missing 'profit_simulation.full' block — "
            "was the model trained recently?"
        )

    p = sim.get("pct_profitable_overall")
    avg_buy = sim.get("avg_pnl_per_buy")
    avg_sell = sim.get("avg_pnl_per_sell")
    if p is None or avg_buy is None or avg_sell is None:
        raise RuntimeError(
            "metrics_6h_v2.json missing required Kelly inputs in profit_simulation.full "
            "(need pct_profitable_overall, avg_pnl_per_buy, avg_pnl_per_sell) — "
            "was the model trained recently?"
        )

    p = float(p)
    mags = (abs(float(avg_buy)), abs(float(avg_sell)))
    avg_win, avg_loss = max(mags), min(mags)

    if avg_loss <= 0:
        log.warning(
            "avg_loss=0 in profit_simulation.full (perfect winning side); "
            "capping b at %.1f as a defensive fallback", DEFAULT_B_CEILING,
        )
        b = DEFAULT_B_CEILING
    else:
        b = avg_win / avg_loss
        if b > DEFAULT_B_CEILING:
            log.warning(
                "computed b=%.2f exceeds defensive ceiling of %.1f — capping",
                b, DEFAULT_B_CEILING,
            )
            b = DEFAULT_B_CEILING
        elif b < DEFAULT_B_FLOOR:
            log.warning(
                "computed b=%.4f below defensive floor of %.2f — capping",
                b, DEFAULT_B_FLOOR,
            )
            b = DEFAULT_B_FLOOR

    return KellyInputs(p=p, b=b, avg_win=avg_win, avg_loss=avg_loss)
