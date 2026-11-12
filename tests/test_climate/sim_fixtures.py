"""Synthetic GTM paper payloads for the simulation tests."""

from __future__ import annotations

import numpy as np
from scipy.stats import norm


def ladder_quotes(strikes, mu: float, sigma: float, *, ceiling: float = 1.0,
                  half_spread: float = 0.015) -> list[tuple[float, float, float]]:
    """(strike, bid, ask) around ceiling * P(Y > k), on the tick grid, inside 0.01..0.99."""
    out = []
    for k in strikes:
        mid = ceiling * norm.sf((k - mu) / sigma)
        bid = float(np.clip(np.round(mid - half_spread, 2), 0.01, 0.98))
        ask = float(np.clip(np.round(mid + half_spread, 2), bid + 0.01, 0.99))
        out.append((float(k), bid, ask))
    return out


def payload(snapshot: str, ladders: dict[str, list[tuple[float, float, float]]],
            models: dict[str, tuple[float, float]], inits: dict[str, str] | None = None) -> dict:
    """A gtm_signals.json-shaped payload."""
    signals = []
    for target, quotes in ladders.items():
        mu, sigma = models[target]
        for k, bid, ask in quotes:
            signals.append({
                "target_month": target, "strike": k, "bid": bid, "ask": ask,
                "yes_mid": (bid + ask) / 2, "p_model": float(norm.sf((k - mu) / sigma)),
                "suspect_quote": bool(ask - bid > 0.10),
            })
    return {
        "ts_utc": snapshot, "sweep_ts_utc": snapshot, "model_fingerprint": "f" * 16,
        "inits": inits or dict.fromkeys(ladders, "2026-10"),
        "predictive_targets": {t: {"mu": m, "sigma": s} for t, (m, s) in models.items()},
        "signals": signals,
    }
