"""Fixtures for the Spark parity tooling tests (analysis/spark_parity.py).

A synthetic quant-data root with the layout the tooling assumes:
ibkr_ticks/quotes/date=*/quotes-HHMMSSZ.parquet (recorder-shaped, the 13
SWEEP_SCHEMA columns), climate_data/labels/labels_ledger.parquet
(climate.storage LEDGER_COLUMNS), climate_data/models/gtm_emos.json (a
one-lead identity EMOS model) and the two reduced CDS products that
build_training_frame reads. The sweep columns are spelled out here on
purpose so these tests never import the recorder, which drags in the venue
layer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import pytest

from climate.emos import EmosModel, LeadParams
from climate.storage import LEDGER_COLUMNS

SWEEP_COLUMNS = [
    "ts_utc", "con_id", "symbol", "expiry", "strike", "right", "bid", "ask",
    "last", "close", "bid_size", "ask_size", "data_type",
]
SWEEP_TS = "2026-09-28T22:08:22Z"
SWEEP_REL = Path("ibkr_ticks") / "quotes" / "date=2026-09-28" / "quotes-220822Z.parquet"


class SparkRoot(NamedTuple):
    root: Path
    sweep: Path
    ledger: Path
    model: Path
    seas5: Path
    era5: Path


def _sweep_rows(ts_utc: str = SWEEP_TS) -> list[dict]:
    """One GTM Dec-2026 market: YES leg con_id 1, NO leg con_id 2."""
    base = dict(
        ts_utc=ts_utc, symbol="GTM", expiry="20261231", strike=1.2,
        last=float("nan"), close=float("nan"), bid_size=10.0, ask_size=10.0,
        data_type="delayed",
    )
    return [
        {**base, "con_id": 1, "right": "C", "bid": 0.40, "ask": 0.44},
        {**base, "con_id": 2, "right": "P", "bid": 0.55, "ask": 0.59},
    ]


def _write_sweep(path: Path, ts_utc: str = SWEEP_TS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_sweep_rows(ts_utc), columns=SWEEP_COLUMNS).to_parquet(path, index=False)
    return path


def _identity_model() -> EmosModel:
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(
        lead_month=1, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1
    )
    return model


@pytest.fixture
def spark_root(tmp_path: Path) -> SparkRoot:
    root = tmp_path / "quant_data"
    sweep = _write_sweep(root / SWEEP_REL)
    climate = root / "climate_data"
    ledger = climate / "labels" / "labels_ledger.parquet"
    ledger.parent.mkdir(parents=True)
    pd.DataFrame(
        [["gtm", "2026-07", 1.18, "2026-08-15T00:00:00Z", "live_first_print", False, "u"]],
        columns=LEDGER_COLUMNS,
    ).to_parquet(ledger, index=False)
    model = climate / "models" / "gtm_emos.json"
    model.parent.mkdir(parents=True)
    _identity_model().to_json(model)
    seas5 = climate / "forecasts" / "seas5_global_mean.parquet"
    seas5.parent.mkdir(parents=True)
    pd.DataFrame(
        {"init": ["2026-08"], "lead": [1], "member": [0], "value_K": [288.1]}
    ).to_parquet(seas5, index=False)
    era5 = climate / "reference" / "era5_global_mean.parquet"
    era5.parent.mkdir(parents=True)
    pd.DataFrame({"month": ["2026-07"], "value_K": [288.0]}).to_parquet(era5, index=False)
    return SparkRoot(root=root, sweep=sweep, ledger=ledger, model=model, seas5=seas5, era5=era5)


@pytest.fixture
def sweep_writer() -> Callable[[Path, str, str], Path]:
    """(root, 'YYYY-MM-DD', 'HHMMSS') -> a new recorder-shaped sweep parquet."""

    def _write(root: Path, date: str, hhmmss: str) -> Path:
        ts = f"{date}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}Z"
        rel = Path("ibkr_ticks") / "quotes" / f"date={date}" / f"quotes-{hhmmss}Z.parquet"
        return _write_sweep(root / rel, ts)

    return _write


@pytest.fixture
def hourly_writer() -> Callable[[Path, str, str], Path]:
    """(root, 'YYYY-MM-DD', name) -> a small parquet under quotes_hourly/date=.../."""

    def _write(root: Path, date: str, name: str) -> Path:
        path = root / "ibkr_ticks" / "quotes_hourly" / f"date={date}" / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"con_id": [1], "open": [0.4], "close": [0.41]}).to_parquet(path, index=False)
        return path

    return _write


@pytest.fixture
def emos_identity_model() -> EmosModel:
    return _identity_model()


@pytest.fixture
def dec_2026_frame() -> pd.DataFrame:
    """Training-frame shape used by climate.paper.run_paper, one target month."""
    return pd.DataFrame(
        {
            "target_month": ["2026-12"],
            "lead_month": [1],
            "y_C": [np.nan],
            "fc_mean_C": [1.30],
            "fc_sd_C": [0.12],
            "target_year_frac": [2026.958],
            "month_of_year": [12],
        }
    )


@pytest.fixture
def universe_json(tmp_path: Path) -> Path:
    """Stage-0 snapshot shape (data/ibkr_universe_live.json) for the two legs."""
    contracts = {
        "1": {"conId": 1, "symbol": "GTM", "localSymbol": "GTM_1226_1.2_YES",
              "lastTradeDateOrContractMonth": "20261231"},
        "2": {"conId": 2, "symbol": "GTM", "localSymbol": "GTM_1226_1.2_NO",
              "lastTradeDateOrContractMonth": "20261231"},
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"contracts": contracts}), encoding="utf-8")
    return path
