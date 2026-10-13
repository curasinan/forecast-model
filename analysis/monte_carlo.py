"""Monte Carlo bankroll simulator — variance and ruin for a quarter-Kelly strategy."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "monte_carlo.log"
RESULTS_DIR = REPO_ROOT / "analysis" / "results"
JSON_PATH = RESULTS_DIR / "monte_carlo.json"
CSV_PATH = RESULTS_DIR / "monte_carlo.csv"
RISK_CONFIG_PATH = REPO_ROOT / "config" / "risk_limits.json"

# Frozen defaults

STARTING_BANKROLL = 500.0
TRUE_EDGES: tuple[float, ...] = (0.00, 0.02, 0.05)   # swept, side by side
BELIEVED_EDGE = 0.02                                  # drives sizing (fixed)
KELLY_MULTIPLIER = 0.25
MIN_POSITION_DOLLARS = 5.0
MAX_SINGLE_POSITION_PCT = 0.05
N_BETS = 200               # attempts per run (fill-gated)
N_RUNS = 10_000
SEED = 42

FILL_RATE = 0.0597
ADVERSE_COST = 0.03125
ENTRY_PRICE = 0.50         # even-money contract

RUIN_FLOOR_PCT = 0.20      # ruin = bankroll ever <= 20% of starting

log = logging.getLogger("analysis.monte_carlo")


# Logging


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)


def load_risk_config(path: Path | str = RISK_CONFIG_PATH) -> dict:
    """Kelly multiplier + position caps from config, falling back to frozen defaults."""
    defaults = {
        "kelly_multiplier": KELLY_MULTIPLIER,
        "min_position_dollars": MIN_POSITION_DOLLARS,
        "max_single_position_pct": MAX_SINGLE_POSITION_PCT,
    }
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    return {k: float(raw.get(k, v)) for k, v in defaults.items()}


# Pure helpers (deterministic — unit-tested)


def full_kelly_fraction(edge: float) -> float:
    """Even-money full-Kelly fraction: f* = 2*edge (win_prob = 0.5 + edge, b = 1)."""
    return 2.0 * edge


def position_size(
    bankroll: float,
    believed_edge: float,
    kelly_multiplier: float,
    min_position: float,
    max_pct: float,
    mode: str = "floor",
) -> float:
    """Quarter-Kelly stake for one bet, subject to the min/max position caps."""
    if bankroll <= 0:
        return 0.0
    desired = kelly_multiplier * full_kelly_fraction(believed_edge) * bankroll
    if mode == "gate":
        if desired < min_position:
            return 0.0
        stake = desired
    else:  # floor
        stake = max(desired, min_position)
    stake = min(stake, max_pct * bankroll)
    return min(stake, bankroll)


def drag_fraction(adverse_cost: float, entry_price: float = ENTRY_PRICE) -> float:
    """Adverse drag as a fraction of stake: a $S stake buys S/price contracts."""
    return adverse_cost / entry_price


def max_drawdown(path: np.ndarray) -> float:
    """Peak-relative maximum drawdown of a single bankroll path (a fraction in [0,1])."""
    path = np.asarray(path, dtype=float)
    running_max = np.maximum.accumulate(path)
    drawdowns = (running_max - path) / np.maximum(running_max, 1e-9)
    return float(np.max(drawdowns))


def summarize(
    finals: np.ndarray,
    max_dds: np.ndarray,
    ruined: np.ndarray,
    fills: np.ndarray,
    starting: float,
) -> dict:
    """Distribution metrics across all runs for one scenario."""
    finals = np.asarray(finals, dtype=float)
    return {
        "median_final": float(np.median(finals)),
        "mean_final": float(np.mean(finals)),
        "ci_5_final": float(np.percentile(finals, 5)),
        "ci_25_final": float(np.percentile(finals, 25)),
        "ci_75_final": float(np.percentile(finals, 75)),
        "ci_95_final": float(np.percentile(finals, 95)),
        "prob_below_start": float(np.mean(finals < starting)),
        "prob_profit": float(np.mean(finals > starting)),
        "prob_ruin": float(np.mean(ruined)),
        "median_max_drawdown": float(np.median(max_dds)),
        "p95_max_drawdown": float(np.percentile(max_dds, 95)),
        "mean_fills": float(np.mean(fills)),
    }


# Simulation (vectorized across runs)


def simulate_scenario(
    rng: np.random.Generator,
    true_edge: float,
    *,
    believed_edge: float = BELIEVED_EDGE,
    starting: float = STARTING_BANKROLL,
    n_bets: int = N_BETS,
    n_runs: int = N_RUNS,
    fill_rate: float = FILL_RATE,
    adverse_cost: float = ADVERSE_COST,
    kelly_multiplier: float = KELLY_MULTIPLIER,
    min_position: float = MIN_POSITION_DOLLARS,
    max_pct: float = MAX_SINGLE_POSITION_PCT,
    mode: str = "floor",
    ruin_floor_pct: float = RUIN_FLOOR_PCT,
) -> dict:
    """Simulate ``n_runs`` bankroll paths of ``n_bets`` attempts. Returns arrays."""
    win_prob = 0.5 + true_edge
    drag = drag_fraction(adverse_cost)
    frac = kelly_multiplier * full_kelly_fraction(believed_edge)  # quarter-Kelly fraction
    floor = starting * ruin_floor_pct

    bankroll = np.full(n_runs, starting, dtype=float)
    peak = bankroll.copy()
    max_dd = np.zeros(n_runs, dtype=float)
    fills = np.zeros(n_runs, dtype=np.int64)
    ruined = bankroll <= floor  # false at start (floor < starting)

    for _ in range(n_bets):
        desired = frac * bankroll
        if mode == "gate":
            stake = np.where(desired < min_position, 0.0, desired)
        else:  # floor
            stake = np.maximum(desired, min_position)
        stake = np.minimum(stake, max_pct * bankroll)
        stake = np.minimum(stake, bankroll)

        filled = rng.random(n_runs) < fill_rate
        won = rng.random(n_runs) < win_prob
        fills += filled

        payoff = np.where(won, stake * (1.0 - drag), -stake * (1.0 + drag))
        bankroll = np.maximum(0.0, bankroll + np.where(filled, payoff, 0.0))

        peak = np.maximum(peak, bankroll)
        dd = (peak - bankroll) / np.maximum(peak, 1e-9)
        max_dd = np.maximum(max_dd, dd)
        ruined |= bankroll <= floor

    return {"finals": bankroll, "max_dds": max_dd, "ruined": ruined, "fills": fills}


# Study


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def run_study(
    json_path: Path | str = JSON_PATH,
    csv_path: Path | str = CSV_PATH,
    *,
    starting: float = STARTING_BANKROLL,
    true_edges: tuple[float, ...] = TRUE_EDGES,
    believed_edge: float = BELIEVED_EDGE,
    n_bets: int = N_BETS,
    n_runs: int = N_RUNS,
    fill_rate: float = FILL_RATE,
    adverse_cost: float = ADVERSE_COST,
    mode: str = "floor",
    seed: int | None = SEED,
    config_path: Path | str = RISK_CONFIG_PATH,
) -> dict:
    cfg = load_risk_config(config_path)
    kelly_multiplier = cfg["kelly_multiplier"]
    min_position = cfg["min_position_dollars"]
    max_pct = cfg["max_single_position_pct"]

    frac = kelly_multiplier * full_kelly_fraction(believed_edge)
    drag = drag_fraction(adverse_cost)
    log.info(
        "Monte Carlo: %d runs x %d attempts, bankroll=$%.0f, fill=%.4f, adverse=%.5f "
        "(drag=%.4f/stake), quarter-Kelly frac=%.4f (believed edge %.3f), min=$%.0f "
        "max=%.0f%% [%s mode], seed=%s",
        n_runs, n_bets, starting, fill_rate, adverse_cost, drag, frac, believed_edge,
        min_position, max_pct * 100, mode, seed,
    )
    log.info(
        "Break-even edge = adverse_cost = %.4f; below it every filled bet loses in "
        "expectation.", adverse_cost,
    )

    rng = np.random.default_rng(seed)
    scenarios: list[dict] = []
    for true_edge in true_edges:
        out = simulate_scenario(
            rng, true_edge, believed_edge=believed_edge, starting=starting,
            n_bets=n_bets, n_runs=n_runs, fill_rate=fill_rate, adverse_cost=adverse_cost,
            kelly_multiplier=kelly_multiplier, min_position=min_position, max_pct=max_pct,
            mode=mode,
        )
        metrics = summarize(out["finals"], out["max_dds"], out["ruined"], out["fills"],
                            starting)
        scenarios.append({"true_edge": true_edge, **metrics})

    log.info("Results by true edge (starting bankroll $%.0f):", starting)
    log.info(
        "  %-8s %9s %9s %9s %9s %11s %10s %10s %9s",
        "edge", "median", "p5", "p95", "mean", "P(<start)", "P(ruin)",
        "med_dd", "p95_dd",
    )
    for s in scenarios:
        log.info(
            "  %-8.2f %9.2f %9.2f %9.2f %9.2f %11s %10s %10s %9s",
            s["true_edge"], s["median_final"], s["ci_5_final"], s["ci_95_final"],
            s["mean_final"], _pct(s["prob_below_start"]), _pct(s["prob_ruin"]),
            _pct(s["median_max_drawdown"]), _pct(s["p95_max_drawdown"]),
        )
    log.info("Avg bets filled per run: %.1f of %d attempts (fill rate %.4f).",
             scenarios[0]["mean_fills"], n_bets, fill_rate)

    results = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "config": {
            "starting_bankroll": starting,
            "true_edges": list(true_edges),
            "believed_edge_for_sizing": believed_edge,
            "kelly_multiplier": kelly_multiplier,
            "quarter_kelly_fraction": frac,
            "min_position_dollars": min_position,
            "max_single_position_pct": max_pct,
            "min_position_mode": mode,
            "n_bets_attempts": n_bets,
            "n_runs": n_runs,
            "fill_rate": fill_rate,
            "adverse_cost": adverse_cost,
            "entry_price": ENTRY_PRICE,
            "drag_fraction_of_stake": drag,
            "break_even_edge": adverse_cost,
            "ruin_floor_pct": RUIN_FLOOR_PCT,
            "seed": seed,
            "friction_source": "analysis/results/fill_simulation.json (buy_-0.01 offset)",
            "payout_model": "even-money binary contract bought at $0.50; win_prob = 0.5 + true_edge",
            "notes": (
                "Sizing uses a fixed believed edge, not the swept true edge, so the "
                "zero-edge case still bets. The engine's 20% drawdown circuit-breaker is "
                "NOT applied so ruin below the floor is measurable."
            ),
        },
        "scenarios": scenarios,
    }

    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("Saved results -> %s", json_path)

    fieldnames = [
        "true_edge", "median_final", "mean_final",
        "ci_5_final", "ci_25_final", "ci_75_final", "ci_95_final",
        "prob_below_start", "prob_profit", "prob_ruin",
        "median_max_drawdown", "p95_max_drawdown", "mean_fills",
    ]
    with Path(csv_path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in scenarios:
            writer.writerow({k: s[k] for k in fieldnames})
    log.info("Saved per-scenario CSV -> %s", csv_path)

    return results


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Monte Carlo bankroll simulator (educational).")
    parser.add_argument("--runs", type=int, default=N_RUNS)
    parser.add_argument("--bets", type=int, default=N_BETS, help="attempts per run (fill-gated)")
    parser.add_argument("--bankroll", type=float, default=STARTING_BANKROLL)
    parser.add_argument("--fill-rate", type=float, default=FILL_RATE)
    parser.add_argument("--adverse-cost", type=float, default=ADVERSE_COST)
    parser.add_argument("--believed-edge", type=float, default=BELIEVED_EDGE,
                        help="edge used to size stakes (fixed across scenarios)")
    parser.add_argument("--min-position-mode", choices=("floor", "gate"), default="floor")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    try:
        run_study(
            starting=args.bankroll, believed_edge=args.believed_edge,
            n_bets=args.bets, n_runs=args.runs, fill_rate=args.fill_rate,
            adverse_cost=args.adverse_cost, mode=args.min_position_mode, seed=args.seed,
        )
    except Exception as exc:  # noqa: BLE001
        log.critical("Monte Carlo FAILED: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
