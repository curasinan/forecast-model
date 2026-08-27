"""Staleness gates for the GTM paper chain (audit finding H2).

Covers climate/paper_criteria.py's StalenessGates/expected_latest_init/
check_staleness, climate/paper.py's freshest_init_by_target and the
sweep-ts parsing helper (climate/ladder.py::sweep_ts_from_path), and both
entrypoint refusals (scripts/run_gtm_paper.py, scripts/run_ibkr_paper.py).
No network/Gateway; tmp_path only.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from climate.emos import EmosModel, LeadParams
from climate.ladder import sweep_ts_from_path
from climate.paper import freshest_init_by_target
from climate.paper_criteria import StalenessGates, check_staleness, expected_latest_init

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# expected_latest_init
# --------------------------------------------------------------------------- #


def test_expected_latest_init_day_boundary():
    # SEAS5 arrives ~5th-6th; day 6 the current month's init may not exist
    # yet, day 7 it is expected to.
    assert expected_latest_init(date(2026, 8, 6)) == "2026-07"
    assert expected_latest_init(date(2026, 8, 7)) == "2026-08"


def test_expected_latest_init_year_rollover():
    assert expected_latest_init(date(2026, 1, 3)) == "2025-12"


# --------------------------------------------------------------------------- #
# check_staleness
# --------------------------------------------------------------------------- #


def test_sweep_older_than_max_is_a_reason():
    reasons = check_staleness(
        sweep_ts=NOW - timedelta(minutes=41), signal_ts=None, inits_by_target=None, now=NOW,
    )
    assert any("sweep" in r for r in reasons)


def test_sweep_within_max_is_fine():
    reasons = check_staleness(
        sweep_ts=NOW - timedelta(minutes=39), signal_ts=None, inits_by_target=None, now=NOW,
    )
    assert reasons == []


def test_signal_older_than_max_is_a_reason():
    reasons = check_staleness(
        sweep_ts=None, signal_ts=NOW - timedelta(minutes=91), inits_by_target=None, now=NOW,
    )
    assert any("signal" in r for r in reasons)


def test_signal_within_max_is_fine():
    reasons = check_staleness(
        sweep_ts=None, signal_ts=NOW - timedelta(minutes=89), inits_by_target=None, now=NOW,
    )
    assert reasons == []


def test_stale_init_names_the_target_and_both_months():
    # now=2026-08-25 -> expected latest init '2026-08'; this target's is '2026-07'.
    reasons = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"2026-08": "2026-07"}, now=NOW,
    )
    assert len(reasons) == 1
    assert "2026-08" in reasons[0] and "2026-07" in reasons[0]


def test_all_fresh_is_empty():
    reasons = check_staleness(
        sweep_ts=NOW - timedelta(minutes=5),
        signal_ts=NOW - timedelta(minutes=5),
        inits_by_target={"2026-08": "2026-08"},
        now=NOW,
    )
    assert reasons == []


def test_require_fresh_init_false_skips_the_init_check():
    reasons = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"2026-08": "2026-01"}, now=NOW,
        gates=StalenessGates(require_fresh_init=False),
    )
    assert reasons == []


def test_none_inputs_skip_only_their_own_check():
    """None is the "absent by design" skip -- not something an entrypoint
    that HAS the data should ever pass (see scripts/run_gtm_paper.py and
    scripts/run_ibkr_paper.py, which always pass real values)."""
    assert check_staleness(sweep_ts=None, signal_ts=None, inits_by_target=None, now=NOW) == []


# --------------------------------------------------------------------------- #
# freshest_init_by_target
# --------------------------------------------------------------------------- #


def _frame_two_targets() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "target_month": ["2026-08", "2026-08", "2026-09", "2026-09"],
            "lead_month": [1, 2, 1, 2],
            "init_date": [
                pd.Timestamp("2026-08-01"), pd.Timestamp("2026-07-01"),
                pd.Timestamp("2026-08-01"), pd.Timestamp("2026-07-01"),
            ],
            "y_C": [np.nan, np.nan, np.nan, np.nan],
            "fc_mean_C": [1.10, 1.00, 1.20, 1.05],
            "fc_sd_C": [0.12, 0.18, 0.12, 0.18],
            "target_year_frac": [2026.625, 2026.625, 2026.708, 2026.708],
            "month_of_year": [8, 8, 9, 9],
        }
    )


def test_freshest_init_by_target_picks_the_shortest_leads_init():
    out = freshest_init_by_target(_frame_two_targets())
    assert out == {"2026-08": "2026-08", "2026-09": "2026-08"}


def test_freshest_init_by_target_falls_back_past_an_unusable_shortest_lead():
    # lead=1 for 2026-08 becomes unusable (non-finite fc_mean_C); the
    # shortest-lead-with-a-usable-row is now lead=2, whose init is '2026-07'.
    frame = _frame_two_targets()
    frame.loc[0, "fc_mean_C"] = np.nan
    out = freshest_init_by_target(frame)
    assert out["2026-08"] == "2026-07"
    assert out["2026-09"] == "2026-08"  # unaffected


def test_freshest_init_by_target_returns_empty_without_init_date():
    # A frame missing init_date (e.g. an older/synthetic frame) must not
    # crash run_paper() -- it reports "no known inits", not "stale".
    frame = _frame_two_targets().drop(columns=["init_date"])
    assert freshest_init_by_target(frame) == {}


# --------------------------------------------------------------------------- #
# sweep_ts_from_path
# --------------------------------------------------------------------------- #


def test_sweep_ts_from_path_parses_partition_and_filename():
    path = r"C:\Users\asus\quant_data\ibkr_ticks\quotes\date=2026-08-27\quotes-204638Z.parquet"
    assert sweep_ts_from_path(path) == datetime(2026, 8, 27, 20, 46, 38, tzinfo=timezone.utc)


def test_sweep_ts_from_path_handles_posix_paths():
    path = "/home/x/ibkr_ticks/quotes/date=2026-01-05/quotes-000102Z.parquet"
    assert sweep_ts_from_path(path) == datetime(2026, 1, 5, 0, 1, 2, tzinfo=timezone.utc)


def test_sweep_ts_from_path_none_for_empty_or_unrecognised():
    assert sweep_ts_from_path("") is None
    assert sweep_ts_from_path(None) is None
    assert sweep_ts_from_path("not-a-sweep-path.parquet") is None


# --------------------------------------------------------------------------- #
# scripts/run_gtm_paper.py -- refuses to write a stale sweep's signals
# --------------------------------------------------------------------------- #


def _model_json(path) -> None:
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(
        lead_month=1, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1
    )
    model.to_json(path)


def _write_old_sweep(ticks_root, old_ts: datetime) -> None:
    sweep_dir = ticks_root / "quotes" / f"date={old_ts.strftime('%Y-%m-%d')}"
    sweep_dir.mkdir(parents=True)
    rows = pd.DataFrame(
        [
            dict(ts_utc=old_ts.isoformat(), con_id=101, symbol="GTM", expiry="20260910",
                 strike=0.8, right="C", bid=0.40, ask=0.44, last=0.42, close=0.41,
                 bid_size=10, ask_size=10, data_type="delayed"),
            dict(ts_utc=old_ts.isoformat(), con_id=102, symbol="GTM", expiry="20260910",
                 strike=0.8, right="P", bid=0.54, ask=0.58, last=0.56, close=0.55,
                 bid_size=10, ask_size=10, data_type="delayed"),
        ]
    )
    rows.to_parquet(sweep_dir / old_ts.strftime("quotes-%H%M%SZ.parquet"), index=False)


def test_run_gtm_paper_refuses_a_stale_sweep_and_writes_no_signals_file(
    tmp_path, monkeypatch, capsys
):
    import scripts.run_gtm_paper as run_gtm_paper_mod

    root = tmp_path / "climate_root"
    ticks_root = tmp_path / "ibkr_ticks"
    model_path = tmp_path / "gtm_emos.json"
    _model_json(model_path)

    now = datetime.now(timezone.utc)
    expected_init = expected_latest_init(now.date())
    # A fresh init on an unrelated target month keeps this test isolated to
    # the sweep-staleness reason (the ladder join finds no matching target
    # anyway, since the quotes resolve to '2026-08' via the expiry
    # heuristic -- irrelevant to the gate under test).
    frame = pd.DataFrame(
        {
            "target_month": ["2099-01"],
            "lead_month": [1],
            "init_date": [pd.Timestamp(f"{expected_init}-01")],
            "y_C": [np.nan],
            "fc_mean_C": [1.0],
            "fc_sd_C": [0.1],
            "target_year_frac": [2099.042],
            "month_of_year": [1],
        }
    )
    monkeypatch.setattr(run_gtm_paper_mod, "build_training_frame", lambda root: frame)

    _write_old_sweep(ticks_root, now - timedelta(hours=2))

    rc = run_gtm_paper_mod.main(
        [
            "--root", str(root),
            "--model", str(model_path),
            "--ticks-root", str(ticks_root),
            "--universe-json", str(tmp_path / "no_universe.json"),
        ]
    )

    assert rc == 1
    assert not (root / "paper" / "gtm_signals.json").exists()
    captured = capsys.readouterr()
    assert "sweep" in captured.err


# --------------------------------------------------------------------------- #
# scripts/run_ibkr_paper.py -- refuses an old-format payload before the
# adapter is ever constructed
# --------------------------------------------------------------------------- #


def test_run_ibkr_paper_refuses_old_format_payload_before_adapter_construction(
    tmp_path, monkeypatch, capsys
):
    import scripts.run_ibkr_paper as run_ibkr_paper_mod

    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.setenv("IBKR_TRADING_MODE", "paper")

    class _RaisingAdapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "IBKRAdapter must never be constructed for a stale/old-format payload"
            )

    monkeypatch.setattr(run_ibkr_paper_mod, "IBKRAdapter", _RaisingAdapter)

    signals_path = tmp_path / "gtm_signals.json"
    signals_path.write_text(
        json.dumps(
            {
                # Old format: written before this task -- no sweep_ts_utc, no inits.
                "ts_utc": "2020-01-01T00:00:00Z",
                "n_quotes": 0,
                "n_ladder": 0,
                "n_tradeable": 0,
                "predictive_targets": {},
                "go_live": {"passed": False, "reasons": [], "metrics": {}},
                "signals": [],
            }
        ),
        encoding="utf-8",
    )

    rc = run_ibkr_paper_mod.main(["--signals", str(signals_path), "--simulate"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "sweep_ts_utc" in captured.err
    assert "inits" in captured.err
