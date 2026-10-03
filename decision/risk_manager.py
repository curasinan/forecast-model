"""Portfolio-level risk limits applied after position sizing.

Four rules, in order:

1. **Drawdown circuit breaker** — if realized P&L pushes current bankroll
   ≥ 20% below the starting bankroll, halt all new trades.
2. **Cluster exposure cap** — per-cluster combined (open + new) dollars
   capped at 10% of bankroll; new signals proportionally scaled.
3. **Total exposure cap** — combined dollars across the whole portfolio
   capped at 35% of bankroll; new signals proportionally scaled.
4. **Max simultaneous positions** — at most 12 concurrent positions; keep
   the highest-edge new signals when the cap is binding.

Drawdown is computed off **realized** P&L only — open paper positions are
not marked to market in Phase 4. Phase 5's reviewers should add MtM if
they want the breaker to fire on unrealized losses as well.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from features.correlation_features import load_clusters

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "polymarket.db"
DEFAULT_CLUSTER_CONFIG_PATH = REPO_ROOT / "config" / "market_clusters.json"
DEFAULT_RISK_CONFIG_PATH = REPO_ROOT / "config" / "risk_limits.json"

DEFAULT_RISK_CONFIG: dict = {
    "starting_bankroll":           1000,
    "drawdown_halt_pct":            0.20,
    "cluster_exposure_cap_pct":     0.10,
    "total_exposure_cap_pct":       0.35,
    "max_simultaneous_positions":   12,
    "min_position_dollars":         5,
    "max_single_position_pct":      0.05,
    "min_edge_threshold":           0.005,
    "kelly_multiplier":             0.25,
    "max_snapshot_age_minutes":     45,
}

log = logging.getLogger("decision.risk_manager")


# --------------------------------------------------------------------------- #
# Schema + config
# --------------------------------------------------------------------------- #


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent CREATE for paper_positions and its indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id        TEXT NOT NULL,
            direction        TEXT NOT NULL,
            entry_price      REAL,
            position_dollars REAL NOT NULL,
            cluster          TEXT,
            opened_at        TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'open',
            closed_at        TEXT,
            realized_pnl     REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_positions_status "
        "ON paper_positions (status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_positions_cluster "
        "ON paper_positions (cluster)"
    )
    conn.commit()


def load_risk_config(path: Path | str = DEFAULT_RISK_CONFIG_PATH) -> dict:
    """Load the risk-limits JSON, creating it with defaults if missing."""
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_RISK_CONFIG, indent=2), encoding="utf-8")
        log.info("Created default risk-limits file at %s", p)
        return dict(DEFAULT_RISK_CONFIG)
    try:
        with p.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"risk-limits file at {p} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"risk-limits file at {p} must contain a JSON object")
    # Fill in any missing keys with defaults so partial configs still work.
    merged = {**DEFAULT_RISK_CONFIG, **loaded}
    return merged


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def apply_risk_limits(
    sized_df: pd.DataFrame,
    bankroll: float,
    db_path: Path | str = DEFAULT_DB_PATH,
    cluster_config_path: Path | str = DEFAULT_CLUSTER_CONFIG_PATH,
    risk_config_path: Path | str = DEFAULT_RISK_CONFIG_PATH,
) -> pd.DataFrame:
    """Apply the four risk rules. Returns the input DataFrame plus a
    ``rejected_reason`` column with one of ``''`` (survivor), ``'drawdown'``,
    ``'cluster_cap'``, ``'total_cap'``, ``'max_concurrent'``.

    The final ``position_dollars`` reflects any rule-2/rule-3 proportional
    scaling. The final ``tradeable`` flag reflects the post-risk verdict.
    """
    cfg = load_risk_config(risk_config_path)
    result = sized_df.copy()
    if "rejected_reason" not in result.columns:
        result["rejected_reason"] = ""

    if result.empty:
        log.info("risk-manager: empty input, nothing to do")
        return result
    if "tradeable" not in result.columns:
        raise RuntimeError(
            "sized_df must contain a 'tradeable' column (position_sizer output)"
        )

    starting_bankroll = float(cfg["starting_bankroll"])

    # ---- DB-backed state: open positions + realized P&L ------------------ #
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        open_rows = conn.execute(
            "SELECT market_id, direction, position_dollars, cluster "
            "FROM paper_positions WHERE status='open'"
        ).fetchall()
        realized_sum_row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_positions WHERE status='closed'"
        ).fetchone()
    finally:
        conn.close()

    realized_pnl = float(realized_sum_row[0] if realized_sum_row else 0.0)
    current_bankroll = starting_bankroll + realized_pnl
    drawdown_pct = (
        (starting_bankroll - current_bankroll) / starting_bankroll
        if starting_bankroll > 0 else 0.0
    )

    # ---- Cluster map: market_id -> cluster_name -------------------------- #
    market_to_cluster = _build_market_to_cluster_map(
        cluster_config_path, market_ids=result["market_id"].astype(str).tolist(),
    )

    # ---- Existing-exposure aggregations ---------------------------------- #
    existing_total = sum(float(row[2]) for row in open_rows)
    existing_by_cluster: dict[str, float] = {}
    for _, _, pd_dollars, cluster in open_rows:
        cluster_key = cluster if cluster else "_singleton_unknown"
        existing_by_cluster[cluster_key] = (
            existing_by_cluster.get(cluster_key, 0.0) + float(pd_dollars)
        )

    log.info(
        "risk-manager summary: starting=$%.2f current=$%.2f drawdown=%.2f%% "
        "(open positions: %d totaling $%.2f; realized P&L: $%+.2f)",
        starting_bankroll, current_bankroll, drawdown_pct * 100,
        len(open_rows), existing_total, realized_pnl,
    )

    # ---- Rule 1: drawdown circuit breaker -------------------------------- #
    if drawdown_pct >= cfg["drawdown_halt_pct"]:
        log.critical(
            "Drawdown circuit breaker tripped (%.2f%% drawdown >= %.2f%% halt) — "
            "rejecting ALL new trades",
            drawdown_pct * 100, cfg["drawdown_halt_pct"] * 100,
        )
        result.loc[result["tradeable"], "rejected_reason"] = "drawdown"
        result["tradeable"] = False
        result["position_dollars"] = 0.0
        return result
    else:
        log.info("rule 1 (drawdown circuit breaker):  PASS")

    # ---- Rule 2: cluster exposure cap ------------------------------------ #
    cluster_cap = float(cfg["cluster_exposure_cap_pct"]) * bankroll
    min_pos = float(cfg["min_position_dollars"])

    # Annotate each row with its cluster_key (for later passes too).
    result["_cluster_key"] = result["market_id"].astype(str).apply(
        lambda m: market_to_cluster.get(m, f"_singleton_{m}")
    )

    n_cluster_scaled = 0
    clusters_touched = result.loc[result["tradeable"], "_cluster_key"].unique().tolist()
    for cluster_key in clusters_touched:
        existing = existing_by_cluster.get(cluster_key, 0.0)
        in_cluster_mask = (
            (result["_cluster_key"] == cluster_key) & result["tradeable"]
        )
        new_sum = float(result.loc[in_cluster_mask, "position_dollars"].sum())
        if new_sum <= 0:
            continue
        if existing + new_sum > cluster_cap:
            headroom = max(0.0, cluster_cap - existing)
            scale = headroom / new_sum if new_sum > 0 else 0.0
            result.loc[in_cluster_mask, "position_dollars"] *= scale
            n_cluster_scaled += int(in_cluster_mask.sum())
            log.info(
                "  cluster %s: existing=$%.2f + new=$%.2f > cap $%.2f → scaled by %.4f",
                cluster_key, existing, new_sum, cluster_cap, scale,
            )

    # Any rows that fell below the $5 floor after scaling become non-tradeable.
    below_floor_after_cluster = (
        result["tradeable"] & (result["position_dollars"] < min_pos)
    )
    n_below_floor_cluster = int(below_floor_after_cluster.sum())
    if n_below_floor_cluster:
        result.loc[below_floor_after_cluster, "tradeable"] = False
        result.loc[below_floor_after_cluster, "position_dollars"] = 0.0
        result.loc[below_floor_after_cluster, "rejected_reason"] = "cluster_cap"

    log.info(
        "rule 2 (cluster cap):                %s",
        f"scaled {n_cluster_scaled} signal(s)"
        + (f", {n_below_floor_cluster} dropped below $%g floor" % min_pos
           if n_below_floor_cluster else "")
        if n_cluster_scaled else "PASS",
    )

    # ---- Rule 3: total exposure cap -------------------------------------- #
    total_cap = float(cfg["total_exposure_cap_pct"]) * bankroll
    new_total = float(result.loc[result["tradeable"], "position_dollars"].sum())
    if existing_total + new_total > total_cap and new_total > 0:
        headroom = max(0.0, total_cap - existing_total)
        scale = headroom / new_total if new_total > 0 else 0.0
        tradeable_mask = result["tradeable"].astype(bool)
        result.loc[tradeable_mask, "position_dollars"] *= scale

        below_floor_after_total = (
            result["tradeable"] & (result["position_dollars"] < min_pos)
        )
        n_below_floor_total = int(below_floor_after_total.sum())
        if n_below_floor_total:
            result.loc[below_floor_after_total, "tradeable"] = False
            result.loc[below_floor_after_total, "position_dollars"] = 0.0
            result.loc[below_floor_after_total, "rejected_reason"] = "total_cap"

        log.info(
            "rule 3 (total exposure cap):         existing=$%.2f + new=$%.2f > cap $%.2f → "
            "scaled all by %.4f (%d below floor dropped)",
            existing_total, new_total, total_cap, scale, n_below_floor_total,
        )
    else:
        log.info("rule 3 (total exposure cap):         PASS")

    # ---- Rule 4: max simultaneous positions ------------------------------ #
    max_positions = int(cfg["max_simultaneous_positions"])
    n_open = len(open_rows)
    n_slots = max(0, max_positions - n_open)
    n_tradeable = int(result["tradeable"].sum())

    if n_slots <= 0:
        # All slots full — reject everything still tradeable.
        rejection_mask = result["tradeable"].astype(bool)
        n_rejected = int(rejection_mask.sum())
        result.loc[rejection_mask, "tradeable"] = False
        result.loc[rejection_mask, "rejected_reason"] = "max_concurrent"
        result.loc[rejection_mask, "position_dollars"] = 0.0
        log.info(
            "rule 4 (max simultaneous):           all %d new rejected (cap %d already filled by open positions)",
            n_rejected, max_positions,
        )
    elif n_tradeable > n_slots:
        # Sort by adjusted_edge desc and keep top n_slots.
        tradeable_idx = result.index[result["tradeable"]].tolist()
        sorted_by_edge = result.loc[tradeable_idx].sort_values(
            "adjusted_edge", ascending=False
        ).index.tolist()
        keep_idx = set(sorted_by_edge[:n_slots])
        reject_idx = [i for i in sorted_by_edge if i not in keep_idx]
        for ri in reject_idx:
            result.at[ri, "tradeable"] = False
            result.at[ri, "position_dollars"] = 0.0
            result.at[ri, "rejected_reason"] = "max_concurrent"
        log.info(
            "rule 4 (max simultaneous):           rejected %d (%d-slot cap with %d already open)",
            len(reject_idx), max_positions, n_open,
        )
    else:
        log.info("rule 4 (max simultaneous):           PASS (%d open + %d new <= %d cap)",
                 n_open, n_tradeable, max_positions)

    n_approved = int(result["tradeable"].sum())
    log.info("approved: %d / %d input signals", n_approved, len(result))

    result = result.drop(columns=["_cluster_key"])
    return result


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _build_market_to_cluster_map(
    cluster_config_path: Path | str,
    market_ids: list[str],
) -> dict[str, str]:
    """Invert ``cluster_name -> [market_ids]`` into ``market_id -> cluster_name``.

    Markets not in any cluster are absent from the map; the caller falls back
    to ``_singleton_{market_id}`` as the cluster key, which guarantees each
    such market gets its own bucket (rule-2 then runs per-bucket as required).
    """
    try:
        clusters = load_clusters(cluster_config_path)
    except FileNotFoundError:
        log.warning(
            "cluster config not found at %s — treating every market as its own singleton cluster",
            cluster_config_path,
        )
        return {}

    mapping: dict[str, str] = {}
    for cluster_name, members in clusters.items():
        for member in members:
            mapping[str(member)] = cluster_name
    return mapping
