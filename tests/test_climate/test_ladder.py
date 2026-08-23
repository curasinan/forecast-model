"""Strike-ladder probabilities and recorder-quote join. No network, no IBKR SDK."""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from climate.ladder import (
    LADDER_COLUMNS,
    SOURCE_EXPIRY_HEURISTIC,
    SOURCE_LABEL,
    ladder_table,
    latest_gtm_quotes,
    list_sweep_files,
    load_contract_labels,
    p_yes,
    target_month_for_contract,
    target_month_from_contract_month,
    target_month_from_expiry,
    target_month_from_local_symbol,
)
from src.domain.fees import FlatCommissionFeeModel


def test_p_yes_strictly_greater_and_half_at_the_mean():
    assert p_yes(1.0, mu=1.0, sigma=0.2) == pytest.approx(0.5)
    # Continuous form: P(X > k) = 1 - Phi((k-mu)/sigma)
    assert p_yes(1.2, mu=1.0, sigma=0.2) == pytest.approx(float(norm.sf((1.2 - 1.0) / 0.2)))
    hi = p_yes(-1.0, mu=1.0, sigma=0.2)
    lo = p_yes(3.0, mu=1.0, sigma=0.2)
    assert hi > 0.99 and lo < 0.01
    arr = p_yes([0.5, 1.0, 1.5], mu=1.0, sigma=0.25)
    assert list(arr) == pytest.approx([p_yes(0.5, 1.0, 0.25), 0.5, p_yes(1.5, 1.0, 0.25)])


def test_p_yes_resolution_shifts_threshold_half_a_print():
    pure = p_yes(1.10, mu=1.10, sigma=0.2)
    rounded = p_yes(1.10, mu=1.10, sigma=0.2, resolution=0.01)
    assert pure == pytest.approx(0.5)
    # Tie at the printed value becomes NO under strictly-greater + 0.01 rounding.
    assert rounded < pure
    assert rounded == pytest.approx(p_yes(1.105, mu=1.10, sigma=0.2))


def test_p_yes_rejects_bad_sigma():
    with pytest.raises(ValueError):
        p_yes(1.0, mu=1.0, sigma=0.0)
    with pytest.raises(ValueError):
        p_yes(1.0, mu=float("nan"), sigma=0.2)


def test_target_month_prefers_local_symbol_over_expiry_heuristic():
    assert target_month_from_local_symbol("GTM_0726_0.6_YES") == "2026-07"
    assert target_month_from_local_symbol("GTM") is None
    assert target_month_from_contract_month("202612") == "2026-12"
    # GTM_1226 expires 2027-02-12: the expiry heuristic maps to 2027-01, which is wrong.
    assert target_month_from_expiry("20270212") == "2027-01"
    tm, src = target_month_for_contract(
        local_symbol="GTM_1226_0.5_YES", expiry="20270212"
    )
    assert (tm, src) == ("2026-12", SOURCE_LABEL)
    tm, src = target_month_for_contract(expiry="20260910")
    assert (tm, src) == ("2026-08", SOURCE_EXPIRY_HEURISTIC)


def _sweep_rows() -> pd.DataFrame:
    """Two GTM strikes for 2026-08, each with YES (C) and NO (P)."""
    ts = "2026-08-23T12:00:00Z"
    rows = [
        # YES 0.8, tight book
        dict(ts_utc=ts, con_id=101, symbol="GTM", expiry="20260910", strike=0.8,
             right="C", bid=0.40, ask=0.44, last=0.42, close=0.41, bid_size=10,
             ask_size=10, data_type="delayed"),
        dict(ts_utc=ts, con_id=102, symbol="GTM", expiry="20260910", strike=0.8,
             right="P", bid=0.54, ask=0.58, last=0.56, close=0.55, bid_size=10,
             ask_size=10, data_type="delayed"),
        # YES 1.2, missing bid (IB sentinel -1)
        dict(ts_utc=ts, con_id=201, symbol="GTM", expiry="20260910", strike=1.2,
             right="C", bid=-1.0, ask=0.20, last=0.18, close=0.19, bid_size=0,
             ask_size=5, data_type="delayed"),
        dict(ts_utc=ts, con_id=202, symbol="GTM", expiry="20260910", strike=1.2,
             right="P", bid=0.78, ask=0.82, last=0.80, close=0.80, bid_size=5,
             ask_size=5, data_type="delayed"),
        # a non-GTM row must be dropped
        dict(ts_utc=ts, con_id=999, symbol="ACD", expiry="20270105", strike=430.0,
             right="C", bid=0.10, ask=0.12, last=0.11, close=0.11, bid_size=1,
             ask_size=1, data_type="delayed"),
    ]
    return pd.DataFrame(rows)


def test_latest_gtm_quotes_joins_no_leg_and_maps_label(tmp_path):
    ticks = tmp_path / "ibkr_ticks"
    sweep_dir = ticks / "quotes" / "date=2026-08-23"
    sweep_dir.mkdir(parents=True)
    path = sweep_dir / "quotes-120000Z.parquet"
    _sweep_rows().to_parquet(path, index=False)

    db = ticks / "contracts_cache.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE contracts (con_id INTEGER PRIMARY KEY, symbol TEXT, "
        "expiry TEXT, local_symbol TEXT)"
    )
    conn.executemany(
        "INSERT INTO contracts VALUES (?, ?, ?, ?)",
        [
            (101, "GTM", "20260910", "GTM_0826_0.8_YES"),
            (102, "GTM", "20260910", "GTM_0826_0.8_NO"),
            (201, "GTM", "20260910", "GTM_0826_1.2_YES"),
            (202, "GTM", "20260910", "GTM_0826_1.2_NO"),
        ],
    )
    conn.commit()
    conn.close()

    files = list_sweep_files(ticks)
    assert files[-1] == path
    q = latest_gtm_quotes(ticks)
    assert len(q) == 2
    row = q[q["strike"] == 0.8].iloc[0]
    assert row["con_id"] == 101
    assert row["no_con_id"] == 102
    assert row["yes_mid"] == pytest.approx(0.42)
    assert row["no_mid"] == pytest.approx(0.56)
    assert row["local_symbol"] == "GTM_0826_0.8_YES"
    assert row["target_month"] == "2026-08"
    assert row["target_month_source"] == SOURCE_LABEL
    # sentinel -1 bid became NaN
    wide = q[q["strike"] == 1.2].iloc[0]
    assert np.isnan(wide["bid"]) and np.isnan(wide["yes_mid"])


def test_load_contract_labels_from_universe_json(tmp_path):
    snap = tmp_path / "universe.json"
    snap.write_text(
        json.dumps(
            {
                "contracts": {
                    "1": {
                        "conId": 11,
                        "symbol": "GTM",
                        "localSymbol": "GTM_0926_-1.5_YES",
                        "lastTradeDateOrContractMonth": "20261009",
                    },
                    "2": {"conId": 12, "symbol": "ACD", "localSymbol": "ACD_2025_420_YES"},
                }
            }
        ),
        encoding="utf-8",
    )
    labels = load_contract_labels(universe_json=snap)
    assert list(labels["con_id"]) == [11]
    assert labels.iloc[0]["local_symbol"] == "GTM_0926_-1.5_YES"


def test_ladder_table_edge_fee_and_suspect_flag():
    quotes = pd.DataFrame(
        {
            "con_id": [1, 2],
            "target_month": ["2026-08", "2026-08"],
            "strike": [0.8, 1.2],
            "bid": [0.40, np.nan],
            "ask": [0.44, 0.20],
            "yes_mid": [0.42, np.nan],
            "no_mid": [0.56, 0.80],
            "spread": [0.04, np.nan],
            "target_month_source": [SOURCE_LABEL, SOURCE_LABEL],
        }
    )
    # N(1.0, 0.2): P(X>0.8) = Phi((1-0.8)/0.2) survival
    mu, sigma = 1.0, 0.2
    table = ladder_table(
        quotes,
        {"2026-08": (mu, sigma)},
        fee_model=FlatCommissionFeeModel(0.01),
    )
    assert list(table.columns) == LADDER_COLUMNS
    tight = table[table["strike"] == 0.8].iloc[0]
    p = p_yes(0.8, mu, sigma)
    assert tight["p_model"] == pytest.approx(p)
    assert tight["edge"] == pytest.approx(p - 0.42)
    assert tight["ev_per_contract_after_fee"] == pytest.approx((p - 0.42) - 0.01)
    assert bool(tight["suspect_quote"]) is False
    missing = table[table["strike"] == 1.2].iloc[0]
    assert bool(missing["suspect_quote"]) is True
    # Target months without a predictive entry are dropped.
    empty = ladder_table(quotes, {"2027-01": (1.0, 0.2)})
    assert empty.empty and list(empty.columns) == LADDER_COLUMNS
