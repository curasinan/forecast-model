"""Bridge (ERA5 K -> NOAA degC) and training-frame assembly. Synthetic data only."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from climate.bridge import Bridge, to_year_month_index
from climate.dataset import (
    FRAME_COLUMNS,
    LEAD1_OFFSET_MONTHS,
    aggregate_members,
    build_frame_from_inputs,
    build_training_frame,
    frame_bridge,
    month_of_year,
    target_month,
    target_year_frac,
)
from climate.noaa import LabelObservation
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET
from climate.storage import append_observations

TRUE_BETA = 0.9
TRUE_ALPHA = {m: -259.0 + 0.8 * np.sin(2 * np.pi * (m - 1) / 12.0) for m in range(1, 13)}


def _months(start="1993-01", end="2025-12") -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="MS")


def _synthetic_era5(idx: pd.DatetimeIndex, seed=0) -> pd.Series:
    rng = np.random.default_rng(seed)
    t = (idx.year - 1993) + (idx.month - 0.5) / 12.0
    seasonal = 1.8 * np.cos(2 * np.pi * (idx.month - 7) / 12.0)  # NH-summer peak
    x = 287.0 + seasonal + 0.02 * t + rng.normal(0, 0.12, len(idx))
    return pd.Series(x, index=idx, name="t2m_global_K")


def _noaa_from_era5(era5: pd.Series, noise_sd: float, seed=1) -> pd.Series:
    rng = np.random.default_rng(seed)
    alpha = np.array([TRUE_ALPHA[m] for m in era5.index.month])
    y = alpha + TRUE_BETA * era5.to_numpy() + rng.normal(0, noise_sd, len(era5))
    return pd.Series(y, index=to_year_month_index(era5.index), name="y_C")


def test_bridge_recovers_exact_params_without_noise():
    era5 = _synthetic_era5(_months())
    noaa = _noaa_from_era5(era5, noise_sd=0.0)
    b = Bridge().fit(era5, noaa)
    assert b.is_fitted
    assert b.beta == pytest.approx(TRUE_BETA, abs=1e-6)
    for m in range(1, 13):
        assert b.alpha[m] == pytest.approx(TRUE_ALPHA[m], abs=1e-3)
    assert b.r2 == pytest.approx(1.0, abs=1e-9)
    assert b.resid_sd == pytest.approx(0.0, abs=1e-6)
    assert b.n_obs == 396
    assert (b.fit_start, b.fit_end) == ("1993-01", "2025-12")


def test_bridge_noisy_fit_reports_resid_sd_and_predicts():
    era5 = _synthetic_era5(_months())
    noaa = _noaa_from_era5(era5, noise_sd=0.03)
    b = Bridge().fit(era5, noaa)
    assert b.beta == pytest.approx(TRUE_BETA, abs=0.03)
    assert 0.02 < b.resid_sd < 0.04  # ~ the injected noise
    assert b.r2 > 0.95
    # predictions match the true line to well within the noise
    x = np.array([286.0, 288.5])
    m = np.array([1, 7])
    truth = np.array([TRUE_ALPHA[1] + TRUE_BETA * 286.0, TRUE_ALPHA[7] + TRUE_BETA * 288.5])
    assert np.allclose(b.predict_mean(x, m), truth, atol=0.02)
    assert isinstance(b.predict_mean(286.0, 1), float)
    assert b.predict_sd_scale() == pytest.approx(abs(b.beta))


def test_bridge_accepts_string_month_index_and_restricts_window():
    era5 = _synthetic_era5(_months())
    era5_str = pd.Series(era5.to_numpy(), index=to_year_month_index(era5.index))
    noaa = _noaa_from_era5(era5, noise_sd=0.0)
    b = Bridge().fit(era5_str, noaa, start="2000-01", end="2010-12")
    assert b.n_obs == 132
    assert (b.fit_start, b.fit_end) == ("2000-01", "2010-12")
    assert b.beta == pytest.approx(TRUE_BETA, abs=1e-6)


def test_bridge_json_roundtrip():
    era5 = _synthetic_era5(_months())
    noaa = _noaa_from_era5(era5, noise_sd=0.02)
    b = Bridge().fit(era5, noaa)
    text = b.to_json()
    d = json.loads(text)
    assert set(d["alpha"]) == {str(m) for m in range(1, 13)}
    b2 = Bridge.from_json(text)
    assert b2.beta == b.beta
    assert b2.alpha == b.alpha
    assert b2.resid_sd == b.resid_sd and b2.r2 == b.r2 and b2.n_obs == b.n_obs
    x = np.linspace(285, 289, 12)
    m = np.arange(1, 13)
    assert np.array_equal(b2.predict_mean(x, m), b.predict_mean(x, m))
    assert b.params["model"].startswith("noaa_gtm_C ~ alpha[month_of_year]")


def test_bridge_errors():
    with pytest.raises(RuntimeError):
        Bridge().predict_mean(287.0, 1)
    with pytest.raises(RuntimeError):
        Bridge().predict_sd_scale()
    era5 = _synthetic_era5(_months("1993-01", "1994-06"))  # no Jul-Dec 1994, but 1993 has all
    noaa = _noaa_from_era5(era5, 0.0)
    Bridge().fit(era5, noaa)  # 18 months, all 12 calendar months present -> ok
    era5_short = _synthetic_era5(_months("1993-01", "1993-11"))
    with pytest.raises(ValueError):
        Bridge().fit(era5_short, _noaa_from_era5(era5_short, 0.0))
    with pytest.raises(ValueError):
        to_year_month_index(["2010-01-01"])  # not 'YYYY-MM'


def test_lead1_is_init_month_convention():
    assert LEAD1_OFFSET_MONTHS == 0
    assert target_month("2010-01-01", 1) == "2010-01"
    assert target_month("2010-01-01", 2) == "2010-02"
    assert target_month("2010-01-01", 6) == "2010-06"
    assert target_month("2010-11-01", 6) == "2011-04"  # crosses the year boundary
    assert target_month(pd.Timestamp("2025-12-01"), 2) == "2026-01"
    vec = target_month(
        pd.Series(pd.to_datetime(["2010-01-01", "2010-12-01", "2010-12-01"])),
        pd.Series([1, 1, 6]),
    )
    assert vec.tolist() == ["2010-01", "2010-12", "2011-05"]


def test_target_year_frac_and_month_of_year():
    assert target_year_frac("2010-01") == pytest.approx(2010.0417, abs=5e-4)
    assert target_year_frac("2010-12") == pytest.approx(2010.9583, abs=5e-4)
    assert target_year_frac(["2010-01", "2011-07"]).tolist() == pytest.approx(
        [2010 + 0.5 / 12, 2011 + 6.5 / 12]
    )
    assert month_of_year("2010-07") == 7
    assert month_of_year(["2010-07", "2011-12"]).tolist() == [7, 12]


def _synthetic_seas5(inits, leads=range(1, 7), n_members=5, seed=2) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for init in inits:
        for lead in leads:
            tm = pd.Period(init, "M") + (lead - 1)
            base = 287.0 + 1.8 * np.cos(2 * np.pi * (tm.month - 7) / 12.0)
            for mem in range(n_members):
                rows.append((pd.Timestamp(init), lead, mem, base + rng.normal(0, 0.1)))
    return pd.DataFrame(rows, columns=["init_date", "lead_month", "member", "t2m_global_K"])


def test_aggregate_members_uses_ddof1():
    seas5 = _synthetic_seas5(["2010-01-01"], leads=[1], n_members=7)
    agg = aggregate_members(seas5)
    vals = seas5["t2m_global_K"].to_numpy()
    assert len(agg) == 1
    assert agg.loc[0, "n_members"] == 7
    assert agg.loc[0, "ens_mean_K"] == pytest.approx(vals.mean())
    assert agg.loc[0, "ens_sd_K"] == pytest.approx(np.std(vals, ddof=1))
    assert agg.loc[0, "ens_sd_K"] != pytest.approx(np.std(vals, ddof=0))


def test_build_frame_from_inputs_schema_and_nan_behaviour():
    era5 = _synthetic_era5(_months("1993-01", "2025-12"))
    noaa = _noaa_from_era5(era5, noise_sd=0.01)
    noaa.loc["2026-01"] = 1.0
    seas5 = _synthetic_seas5(["2025-10-01", "2025-12-01"])
    frame = build_frame_from_inputs(seas5, era5, noaa)

    assert list(frame.columns) == FRAME_COLUMNS
    assert len(frame) == 12
    assert pd.api.types.is_datetime64_any_dtype(frame["init_date"])
    for col in ("lead_month", "month_of_year", "n_members"):
        assert pd.api.types.is_integer_dtype(frame[col]), col
    for col in ("target_year_frac", "ens_mean_K", "ens_sd_K", "era5_K", "y_C", "fc_mean_C", "fc_sd_C"):
        assert pd.api.types.is_float_dtype(frame[col]), col
    assert frame["target_month"].map(lambda s: isinstance(s, str) and len(s) == 7).all()

    row = frame[(frame.init_date == "2025-12-01") & (frame.lead_month == 2)].iloc[0]
    assert row.target_month == "2026-01"
    assert row.month_of_year == 1
    assert row.target_year_frac == pytest.approx(2026 + 0.5 / 12)
    assert np.isnan(row.era5_K)  # ERA5 ends 2025-12
    assert row.y_C == 1.0  # NOAA has 2026-01 already
    assert np.isfinite(row.fc_mean_C) and np.isfinite(row.fc_sd_C)

    # far-future rows: both reference columns NaN, forecast columns populated
    fut = frame[(frame.init_date == "2025-12-01") & (frame.lead_month >= 3)]
    assert fut["era5_K"].isna().all() and fut["y_C"].isna().all()
    assert np.isfinite(fut["fc_mean_C"]).all() and np.isfinite(fut["fc_sd_C"]).all()

    # ERA5 joined on the TARGET month, not the init month
    r = frame[(frame.init_date == "2025-10-01") & (frame.lead_month == 3)].iloc[0]
    assert r.target_month == "2025-12"
    assert r.era5_K == pytest.approx(era5.loc["2025-12-01"])
    assert r.y_C == pytest.approx(noaa.loc["2025-12"])

    # bridge applied consistently
    b = frame_bridge(frame)
    assert isinstance(b, Bridge) and b.is_fitted
    assert np.allclose(
        frame["fc_mean_C"], b.predict_mean(frame["ens_mean_K"].to_numpy(), frame["month_of_year"].to_numpy())
    )
    assert np.allclose(frame["fc_sd_C"], b.predict_sd_scale() * frame["ens_sd_K"])
    assert frame.attrs["bridge_json"] == b.to_json()
    assert (frame["n_members"] == 5).all()


def test_build_frame_uses_supplied_bridge():
    era5 = _synthetic_era5(_months("1993-01", "2025-12"))
    noaa = _noaa_from_era5(era5, noise_sd=0.0)
    seas5 = _synthetic_seas5(["2020-03-01"])
    given = Bridge(alpha={m: 0.0 for m in range(1, 13)}, beta=2.0)
    frame = build_frame_from_inputs(seas5, era5, noaa, bridge=given)
    assert frame_bridge(frame) is given
    assert np.allclose(frame["fc_mean_C"], 2.0 * frame["ens_mean_K"])
    assert np.allclose(frame["fc_sd_C"], 2.0 * frame["ens_sd_K"])


def test_build_training_frame_from_disk(tmp_path):
    root = tmp_path / "climate_data"
    era5 = _synthetic_era5(_months("1993-01", "2025-12"))
    noaa = _noaa_from_era5(era5, noise_sd=0.02)
    seas5 = _synthetic_seas5(["2024-06-01", "2025-11-01"], n_members=3)

    seas5_path = root / SEAS5_PARQUET[0] / SEAS5_PARQUET[1]
    seas5_path.parent.mkdir(parents=True)
    seas5.to_parquet(seas5_path, index=False)
    era5_path = root / ERA5_PARQUET[0] / ERA5_PARQUET[1]
    era5_path.parent.mkdir(parents=True)
    pd.DataFrame({"month": era5.index, "t2m_global_K": era5.to_numpy()}).to_parquet(
        era5_path, index=False
    )
    obs = [LabelObservation("gtm", p, float(v), "https://example.test") for p, v in noaa.items()]
    obs.append(LabelObservation("gt", "2024", 1.2, "https://example.test"))  # other series ignored
    append_observations(obs, backfill=True, root=root)
    # a later revision must NOT change the first-print label used as y_C
    append_observations(
        [LabelObservation("gtm", "2024-06", 99.0, "https://example.test")], backfill=False, root=root
    )

    frame = build_training_frame(root)
    assert list(frame.columns) == FRAME_COLUMNS
    assert len(frame) == 12
    assert (frame["n_members"] == 3).all()
    b = frame_bridge(frame)
    assert b.beta == pytest.approx(TRUE_BETA, abs=0.03)
    assert 0.01 < b.resid_sd < 0.03
    r = frame[(frame.init_date == "2024-06-01") & (frame.lead_month == 1)].iloc[0]
    assert r.target_month == "2024-06"
    assert r.y_C == pytest.approx(noaa.loc["2024-06"])  # first print, not the 99.0 revision
    assert r.era5_K == pytest.approx(era5.loc["2024-06-01"])
    last = frame[(frame.init_date == "2025-11-01") & (frame.lead_month == 6)].iloc[0]
    assert last.target_month == "2026-04"
    assert np.isnan(last.y_C) and np.isnan(last.era5_K) and np.isfinite(last.fc_mean_C)
