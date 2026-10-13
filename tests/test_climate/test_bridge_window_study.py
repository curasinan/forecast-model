"""Bridge robustness study (audit H4): the ERA5->NOAA bridge refit inside each window."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import scripts.study_bridge_window as study_mod
from climate.bridge import Bridge, to_year_month_index
from climate.dataset import build_frame_from_inputs
from climate.emos import expanding_window_scores
from climate.noaa import LabelObservation
from climate.paper_criteria import GoLiveVerdict
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET
from climate.storage import append_observations

LEADS = (1, 2, 3)
FIRST_TEST, LAST_TEST = 2006, 2011
MIN_TRAIN = 12


@pytest.fixture(autouse=True)
def _never_the_real_data_root(tmp_path, monkeypatch):
    """Any default-root resolution lands in tmp, never ~/quant_data/climate_data."""
    guard = tmp_path / "guard_root_must_stay_absent"
    monkeypatch.setenv("CLIMATE_DATA_ROOT", str(guard))
    yield
    assert not guard.exists(), "the study resolved (and created) a default data root"


# synthetic inputs


def _inputs(*, drift_per_year: float, bridge_noise_sd: float = 0.03, seed: int = 0):
    """SEAS5 members / ERA5 K / NOAA degC for 2000-01 .. 2011-12."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2000-01", "2011-12", freq="MS")
    t = (idx.year - 2000) + (idx.month - 0.5) / 12.0
    x_noise = rng.normal(0, 0.06, len(idx))
    era5 = pd.Series(
        287.0 + 1.5 * np.cos(2 * np.pi * (idx.month - 7) / 12.0) + 0.02 * t + x_noise,
        index=idx,
        name="t2m_global_K",
    )
    alpha = np.array([-258.8 + 0.4 * np.sin(2 * np.pi * (m - 1) / 12.0) for m in idx.month])
    y_noise = rng.normal(0, bridge_noise_sd, len(idx)) if bridge_noise_sd else np.zeros(len(idx))
    noaa = pd.Series(
        alpha + 0.95 * era5.to_numpy() + drift_per_year * t + y_noise,
        index=to_year_month_index(idx),
        name="y_C",
    )
    rows = []
    for init in pd.date_range("2000-01-01", "2011-10-01", freq="MS"):
        for lead in LEADS:
            tm = (pd.Period(init, "M") + (lead - 1)).to_timestamp()
            base = float(era5.loc[tm])
            for mem in range(3):
                rows.append((init, lead, mem, base + rng.normal(0, 0.04 * lead)))
    seas5 = pd.DataFrame(rows, columns=["init_date", "lead_month", "member", "t2m_global_K"])
    return seas5, era5, noaa


def _run(seas5, era5, noaa, *, trend=False):
    return study_mod.run_study(
        seas5,
        era5,
        noaa,
        first_test_year=FIRST_TEST,
        last_test_year=LAST_TEST,
        trend=trend,
        leads=LEADS,
        min_train_targets=MIN_TRAIN,
    )


# the brief's Step 1 test


def test_drifting_bridge_gives_distinct_window_params_and_both_skill_columns():
    seas5, era5, noaa = _inputs(drift_per_year=0.04)
    study = _run(seas5, era5, noaa)

    # one bridge per test year, each fit only on months strictly before it
    assert sorted(study.window_bridges) == list(range(FIRST_TEST, LAST_TEST + 1))
    for year, b in study.window_bridges.items():
        assert b.is_fitted
        assert b.fit_end == f"{year - 1}-12"
    betas = np.array([study.window_bridges[y].beta for y in sorted(study.window_bridges)])
    jan = np.array([study.window_bridges[y].alpha[1] for y in sorted(study.window_bridges)])
    assert np.ptp(betas) > 1e-3, betas
    assert np.ptp(jan) > 1e-2, jan
    # and none of them is the production full-history bridge
    assert study.full_bridge.fit_end == "2011-12"
    assert all(abs(b.beta - study.full_bridge.beta) > 1e-4 for b in study.window_bridges.values())

    table = study.comparison
    assert {"skill_vs_trend_leaky", "skill_vs_trend_honest", "skill_delta"} <= set(table.columns)
    assert list(table["lead_month"]) == list(LEADS)
    assert np.allclose(
        table["skill_delta"], table["skill_vs_trend_honest"] - table["skill_vs_trend_leaky"]
    )
    assert isinstance(study.honest_verdict, GoLiveVerdict)
    assert isinstance(study.leaky_verdict, GoLiveVerdict)


# the decomposition is exactly the existing machinery, and leak-free


def test_honest_year_equals_single_year_scoring_with_the_window_bridge():
    seas5, era5, noaa = _inputs(drift_per_year=0.04)
    study = _run(seas5, era5, noaa)
    year = 2009
    wb = Bridge().fit(era5, noaa, start=None, end=f"{year - 1}-12")
    frame = build_frame_from_inputs(seas5, era5, noaa, bridge=wb)
    expected = expanding_window_scores(
        frame, year, year, trend=False, leads=LEADS, min_train_targets=MIN_TRAIN
    )
    got = study.honest_scores[study.honest_scores["test_year"] == year].reset_index(drop=True)
    pd.testing.assert_frame_equal(got, expected.reset_index(drop=True))
    assert study.window_bridges[year].params == wb.params

    full = build_frame_from_inputs(seas5, era5, noaa)
    scored = (
        (np.floor(frame["target_year_frac"]).astype(int) == year)
        & frame["lead_month"].isin(LEADS)
        & np.isfinite(frame["y_C"])
    )
    diff = (frame["fc_mean_C"] - full["fc_mean_C"])[scored]
    row = study.bridge_table().set_index("test_year").loc[year]
    assert row["n_test_rows"] == int(scored.sum()) > 0
    assert row["test_fc_mean_shift_C"] == pytest.approx(float(diff.mean()))
    assert row["test_fc_mean_abs_shift_C"] == pytest.approx(float(diff.abs().mean()))
    assert abs(row["test_fc_mean_shift_C"]) > 1e-3  # drift => the leak is visible


def test_leaky_scores_are_the_production_full_history_path():
    seas5, era5, noaa = _inputs(drift_per_year=0.04)
    study = _run(seas5, era5, noaa)
    full = build_frame_from_inputs(seas5, era5, noaa)  # bridge=None -> full overlap
    expected = expanding_window_scores(
        full, FIRST_TEST, LAST_TEST, trend=False, leads=LEADS, min_train_targets=MIN_TRAIN
    )
    pd.testing.assert_frame_equal(
        study.leaky_scores.reset_index(drop=True), expected.reset_index(drop=True)
    )


def test_window_bridge_ignores_labels_from_the_test_year_onward():
    seas5, era5, noaa = _inputs(drift_per_year=0.04)
    base = _run(seas5, era5, noaa)
    poisoned = noaa.copy()
    poisoned[poisoned.index >= "2009-01"] += 5.0  # garbage from 2009 on
    study = _run(seas5, era5, poisoned)
    for year in (2006, 2007, 2008, 2009):
        assert study.window_bridges[year].params == base.window_bridges[year].params
    # ...while the production full-history bridge swallows the garbage
    assert study.full_bridge.params != base.full_bridge.params


def test_exactly_identified_bridge_makes_honest_equal_leaky():
    seas5, era5, noaa = _inputs(drift_per_year=0.0, bridge_noise_sd=0.0)
    study = _run(seas5, era5, noaa)
    betas = [b.beta for b in study.window_bridges.values()]
    assert np.ptp(betas) < 1e-6
    table = study.comparison
    assert np.all(np.abs(table["skill_vs_trend_leaky"]) < 1.0)  # non-degenerate baseline
    assert np.allclose(table["skill_delta"], 0.0, rtol=0.0, atol=1e-6)
    assert np.allclose(table["crps_emos_honest"], table["crps_emos_leaky"], rtol=0.0, atol=1e-8)
    assert np.allclose(study.bridge_table()["test_fc_mean_abs_shift_C"], 0.0, atol=1e-8)


def test_comparison_is_paired_trend_baseline_is_bridge_free():
    seas5, era5, noaa = _inputs(drift_per_year=0.04)
    study = _run(seas5, era5, noaa, trend=True)
    table = study.comparison
    # the bridge only moves the EMOS inputs: same rows, same baseline CRPS
    assert (table["n_leaky"] == table["n_honest"]).all()
    assert np.allclose(study.leaky_summary["crps_trend"], study.honest_summary["crps_trend"])
    assert np.allclose(table["crps_trend"], study.honest_summary["crps_trend"])
    assert study.trend is True


# CLI: reads a synthetic on-disk root, writes exactly the two --out files


def _write_root(root, seas5, era5, noaa, *, operational_trend: bool | None):
    seas5_path = root / SEAS5_PARQUET[0] / SEAS5_PARQUET[1]
    seas5_path.parent.mkdir(parents=True)
    seas5.to_parquet(seas5_path, index=False)
    era5_path = root / ERA5_PARQUET[0] / ERA5_PARQUET[1]
    era5_path.parent.mkdir(parents=True)
    pd.DataFrame({"month": era5.index, "t2m_global_K": era5.to_numpy()}).to_parquet(
        era5_path, index=False
    )
    obs = [LabelObservation("gtm", p, float(v), "https://example.test") for p, v in noaa.items()]
    append_observations(obs, backfill=True, root=root)
    if operational_trend is not None:
        (root / "models").mkdir()
        (root / "models" / "gtm_emos.json").write_text(
            json.dumps({"version": 1, "trend": operational_trend, "leads": {}}), encoding="utf-8"
        )


def _snapshot(root):
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
    )


@pytest.mark.parametrize(
    ("operational_trend", "headline", "source_hint"),
    [(None, "trend", "assumed"), (False, "notrend", "gtm_emos.json")],
)
def test_cli_writes_markdown_and_json_and_leaves_the_root_untouched(
    tmp_path, operational_trend, headline, source_hint
):
    seas5, era5, noaa = _inputs(drift_per_year=0.04)
    root = tmp_path / "climate_data"
    _write_root(root, seas5, era5, noaa, operational_trend=operational_trend)
    before = _snapshot(root)
    out_md = tmp_path / "out" / "gtm_bridge_robustness.md"
    out_json = tmp_path / "out" / "gtm_bridge_robustness.json"

    rc = study_mod.main(
        [
            "--root", str(root),
            "--out", str(out_md),
            "--json-out", str(out_json),
            "--first-test-year", str(FIRST_TEST),
            "--last-test-year", str(LAST_TEST),
            "--leads", "1,2,3",
            "--min-train-targets", str(MIN_TRAIN),
        ]
    )
    assert rc == 0
    assert _snapshot(root) == before  # read-only on the data root
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        [out_md.name, out_json.name]
    )

    payload = json.loads(out_json.read_text(encoding="utf-8"))  # strict JSON (no NaN)
    assert payload["headline_variant"] == headline
    assert source_hint in payload["operational_trend_source"]
    assert set(payload["variants"]) == {"notrend", "trend"}
    head = payload["variants"][headline]
    assert payload["honest_gate_passed"] is head["honest_verdict"]["passed"]
    assert {r["lead_month"] for r in head["comparison"]} == set(LEADS)
    for rec in head["comparison"]:
        assert "skill_vs_trend_leaky" in rec and "skill_vs_trend_honest" in rec
    assert [w["test_year"] for w in head["window_bridges"]] == list(
        range(FIRST_TEST, LAST_TEST + 1)
    )

    md = out_md.read_text(encoding="utf-8")
    verdict = "PASS" if payload["honest_gate_passed"] else "FAIL"
    assert f"Honest-bridge hindcast gate ({headline} variant): **{verdict}**" in md
    assert "skill vs trend (leaky)" in md and "skill vs trend (honest)" in md
    assert "delta" in md
