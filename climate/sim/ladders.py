"""Ladder snapshots from GTM paper payloads (gtm_signals.json and its history)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import numpy as np
import pandas as pd

SNAPSHOT_COLUMNS = [
    "snapshot_utc",
    "signal_utc",
    "model_fingerprint",
    "target_month",
    "init",
    "strike",
    "bid",
    "ask",
    "mid",
    "spread",
    "suspect_quote",
    "p_model",
    "model_mu",
    "model_sigma",
]


def read_payloads(paths: Iterable[str | Path]) -> list[dict]:
    """Payload dicts from .json files (one each) and .jsonl files (one per line)."""
    payloads: list[dict] = []
    for raw in paths:
        path = Path(raw)
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            for n, line in enumerate(text.splitlines(), start=1):
                if line.strip():
                    try:
                        payloads.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"{path}:{n}: not a JSON line ({exc})") from exc
        else:
            payloads.append(json.loads(text))
    return payloads


def _num(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number if np.isfinite(number) else float("nan")


def snapshot_rows(payload: Mapping) -> pd.DataFrame:
    """One row per quoted strike of one payload, with the model's mu and sigma for its month."""
    snapshot = payload.get("sweep_ts_utc") or payload.get("ts_utc")
    if not snapshot:
        raise ValueError("payload has neither sweep_ts_utc nor ts_utc")
    targets = payload.get("predictive_targets") or {}
    inits = payload.get("inits") or {}
    rows = []
    for s in payload.get("signals") or []:
        tm = str(s.get("target_month"))
        bid, ask = _num(s.get("bid")), _num(s.get("ask"))
        pred = targets.get(tm) or {}
        rows.append({
            "snapshot_utc": pd.Timestamp(snapshot),
            "signal_utc": pd.Timestamp(payload["ts_utc"]) if payload.get("ts_utc") else pd.NaT,
            "model_fingerprint": payload.get("model_fingerprint"),
            "target_month": tm,
            "init": inits.get(tm),
            "strike": _num(s.get("strike")),
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2.0,
            "spread": ask - bid,
            "suspect_quote": bool(s.get("suspect_quote", False)),
            "p_model": _num(s.get("p_model")),
            "model_mu": _num(pred.get("mu")),
            "model_sigma": _num(pred.get("sigma")),
        })
    return pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)


def ladder_frame(payloads: Iterable[Mapping]) -> pd.DataFrame:
    """All snapshots stacked; a strike seen twice in one snapshot keeps the later payload's row."""
    frames = [snapshot_rows(p) for p in payloads]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    for col in ("snapshot_utc", "signal_utc"):
        out[col] = pd.to_datetime(out[col], utc=True)
    out = out.drop_duplicates(["snapshot_utc", "target_month", "strike"], keep="last")
    return out.sort_values(["snapshot_utc", "target_month", "strike"]).reset_index(drop=True)


__all__ = ["SNAPSHOT_COLUMNS", "ladder_frame", "read_payloads", "snapshot_rows"]
