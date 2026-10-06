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
import pytest

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


# Final-review finding C-1: a target month T can only ever have inits <= T
# (SEAS5 leadtime_month=1 IS the init month -- climate/dataset.py), so for a
# current/past target the freshest init that can physically exist is T
# itself. The gate now requires init == min(expected_latest_init(today), T):
# maximally strict, never unsatisfiable.
SEPT_10 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)  # expected init '2026-09'


def test_live_target_before_the_expected_month_passes_with_its_own_init_month():
    # Target 2026-08 checked on Sept 10: its freshest possible init is
    # 2026-08 (lead 1). The old rule demanded '2026-09' -- impossible.
    assert expected_latest_init(SEPT_10.date()) == "2026-09"
    reasons = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"2026-08": "2026-08"}, now=SEPT_10,
    )
    assert reasons == []


def test_live_target_before_the_expected_month_with_a_stale_init_is_refused():
    # Guard catches known-bad: init < min(expected, T) is genuinely stale.
    reasons = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"2026-08": "2026-07"}, now=SEPT_10,
    )
    assert len(reasons) == 1
    assert "2026-08" in reasons[0] and "2026-07" in reasons[0]


def test_future_target_still_requires_the_expected_latest_init():
    # T > expected: min(expected, T) == expected -- unchanged strictness.
    stale = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"2026-12": "2026-08"}, now=SEPT_10,
    )
    assert len(stale) == 1 and "2026-12" in stale[0] and "2026-09" in stale[0]
    fresh = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"2026-12": "2026-09"}, now=SEPT_10,
    )
    assert fresh == []


def test_an_init_later_than_its_own_target_is_refused():
    # Physically impossible (init <= target always) -> a data bug; must not
    # pass just because it is "fresh".
    reasons = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"2026-08": "2026-09"}, now=SEPT_10,
    )
    assert len(reasons) == 1


def test_a_malformed_target_key_gets_no_relaxation():
    # Fail closed: only a clean 'YYYY-MM' target earns the min(expected, T)
    # rule. '1999' would otherwise compare below '2026-09' and let init
    # '1999' through.
    reasons = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target={"1999": "1999"}, now=SEPT_10,
    )
    assert len(reasons) == 1


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


def test_freshest_init_by_target_fitted_leads_excludes_an_unfit_shortest_lead():
    """Safety-asymmetry fix (controller ruling R11): the shortest lead for
    2026-08 (lead=1, init '2026-08') has perfectly finite fc_mean_C/fc_sd_C
    but was never fit by the model. Without fitted_leads this function
    cannot tell and reports the fresher '2026-08' -- exactly the
    dangerous direction, since predictive_by_target_month would actually
    have skipped that row (model.predict gives it NaN) and fallen back to
    lead=2's staler '2026-07' init. Passing fitted_leads=[2] must
    reproduce that real selection.
    """
    frame = _frame_two_targets()
    without_fitted = freshest_init_by_target(frame)
    assert without_fitted["2026-08"] == "2026-08"  # the unsafe/optimistic answer

    with_fitted = freshest_init_by_target(frame, fitted_leads=[2])
    assert with_fitted["2026-08"] == "2026-07"  # matches what the model actually priced off
    assert with_fitted["2026-09"] == "2026-07"  # 2026-09's lead=1 is also excluded


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


def _sweep_rows(ts_iso: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            dict(ts_utc=ts_iso, con_id=101, symbol="GTM", expiry="20260910",
                 strike=0.8, right="C", bid=0.40, ask=0.44, last=0.42, close=0.41,
                 bid_size=10, ask_size=10, data_type="delayed"),
            dict(ts_utc=ts_iso, con_id=102, symbol="GTM", expiry="20260910",
                 strike=0.8, right="P", bid=0.54, ask=0.58, last=0.56, close=0.55,
                 bid_size=10, ask_size=10, data_type="delayed"),
        ]
    )


def _write_old_sweep(ticks_root, old_ts: datetime) -> None:
    sweep_dir = ticks_root / "quotes" / f"date={old_ts.strftime('%Y-%m-%d')}"
    sweep_dir.mkdir(parents=True)
    _sweep_rows(old_ts.isoformat()).to_parquet(
        sweep_dir / old_ts.strftime("quotes-%H%M%SZ.parquet"), index=False
    )


def _write_badly_named_sweep(ticks_root, ts: datetime) -> None:
    """A real, on-disk, well-schemaed sweep whose FILENAME doesn't match
    the recorder's own quotes-HHMMSSZ.parquet convention (Finding 2a)."""
    sweep_dir = ticks_root / "quotes" / f"date={ts.strftime('%Y-%m-%d')}"
    sweep_dir.mkdir(parents=True)
    _sweep_rows(ts.isoformat()).to_parquet(sweep_dir / "quotes_dump.parquet", index=False)


def _fresh_init_frame(now: datetime) -> pd.DataFrame:
    """One row on an unrelated target month with a fresh init, so an
    entrypoint test isolates whatever sweep-related reason is under test
    (the ladder join finds no matching target anyway, since the quotes
    resolve to '2026-08' via the expiry heuristic -- irrelevant here).
    """
    expected_init = expected_latest_init(now.date())
    return pd.DataFrame(
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


def test_run_gtm_paper_refuses_a_stale_sweep_and_writes_no_signals_file(
    tmp_path, monkeypatch, capsys
):
    import scripts.run_gtm_paper as run_gtm_paper_mod

    root = tmp_path / "climate_root"
    ticks_root = tmp_path / "ibkr_ticks"
    model_path = tmp_path / "gtm_emos.json"
    _model_json(model_path)

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        run_gtm_paper_mod, "build_training_frame", lambda root: _fresh_init_frame(now)
    )

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


def test_run_gtm_paper_refuses_when_the_sweep_filename_does_not_parse(
    tmp_path, monkeypatch, capsys
):
    """Finding 2a: a real, FRESH, on-disk sweep whose filename fails
    sweep_ts_from_path's regex must refuse just as loudly as a stale one --
    it must not silently produce sweep_ts_utc=null and sail through.
    """
    import scripts.run_gtm_paper as run_gtm_paper_mod

    root = tmp_path / "climate_root"
    ticks_root = tmp_path / "ibkr_ticks"
    model_path = tmp_path / "gtm_emos.json"
    _model_json(model_path)

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        run_gtm_paper_mod, "build_training_frame", lambda root: _fresh_init_frame(now)
    )

    _write_badly_named_sweep(ticks_root, now)  # fresh timestamp, bad filename

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
    assert "could not parse" in captured.err
    assert "quotes_dump.parquet" in captured.err


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


def test_run_ibkr_paper_refuses_present_but_null_sweep_ts_utc_before_adapter_construction(
    tmp_path, monkeypatch, capsys
):
    """Finding 2b: a NEW-format payload (has both keys) whose sweep_ts_utc
    is null -- e.g. run_gtm_paper.py's own gate would refuse this today,
    but a hand-edited or otherwise-produced file could still carry one --
    must refuse exactly like a missing key, not silently skip the sweep
    check via check_staleness's None-means-absent-by-design path.
    """
    import scripts.run_ibkr_paper as run_ibkr_paper_mod

    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.setenv("IBKR_TRADING_MODE", "paper")

    class _RaisingAdapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "IBKRAdapter must never be constructed for a null sweep_ts_utc payload"
            )

    monkeypatch.setattr(run_ibkr_paper_mod, "IBKRAdapter", _RaisingAdapter)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signals_path = tmp_path / "gtm_signals.json"
    signals_path.write_text(
        json.dumps(
            {
                "ts_utc": now_str,  # fresh signal
                "sweep_ts_utc": None,  # present key, null value -- unparseable sweep name
                "inits": {},  # present, empty -- isolates the test to sweep_ts_utc
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
    assert "inits" not in captured.err  # isolated: only the sweep reason fired


# --------------------------------------------------------------------------- #
# Final-review finding C-1: the init gate is scoped to the PRICED LADDER.
#
# run_paper used to report ``inits`` for every target month in the whole
# training frame -- every resolved (init, lead) row back to 1993 -- and the
# gate demanded the expected latest init of each, so any real frame (which
# always carries resolved history) was refused on every run. Every earlier
# test frame here was all-unresolved, which is why the suite stayed green.
# --------------------------------------------------------------------------- #


def _shift_month(ym: str, k: int) -> str:
    year, month = (int(p) for p in ym.split("-"))
    idx = year * 12 + (month - 1) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _model_leads(*leads: int) -> EmosModel:
    model = EmosModel(trend=False)
    for lead in leads:
        model.params[lead] = LeadParams(
            lead_month=lead, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1
        )
    return model


def _row(target: str, init: str, lead: int, y_c: float = np.nan) -> dict:
    return {
        "target_month": target,
        "lead_month": lead,
        "init_date": pd.Timestamp(f"{init}-01"),
        "y_C": y_c,
        "fc_mean_C": 1.10,
        "fc_sd_C": 0.12,
        "target_year_frac": int(target[:4]) + (int(target[5:]) - 0.5) / 12,
        "month_of_year": int(target[5:]),
    }


def _mixed_history_frame(live_target: str, live_init: str, live_lead: int) -> pd.DataFrame:
    """Resolved 2006 history (y_C known, like every real frame) plus one
    live, unresolved target."""
    return pd.DataFrame([
        _row("2006-01", "2006-01", 1, y_c=0.45),
        _row("2006-02", "2006-01", 2, y_c=0.50),
        _row(live_target, live_init, live_lead),
    ])


def _quotes_for(target: str) -> pd.DataFrame:
    return pd.DataFrame({
        "con_id": [101], "target_month": [target], "strike": [0.6],
        "bid": [0.40], "ask": [0.44], "yes_mid": [0.42], "no_mid": [0.56],
        "spread": [0.04], "suspect_quote": [False],
        "target_month_source": ["label"],
    })


def test_run_paper_inits_cover_only_the_priced_ladder_and_the_gate_passes():
    from climate.paper import run_paper

    frame = _mixed_history_frame("2026-12", "2026-09", 4)
    payload = run_paper(frame, _model_leads(1, 2, 4), quotes_df=_quotes_for("2026-12"))

    assert payload["n_ladder"] == 1
    # The resolved 2006 rows are in the frame (and even in
    # predictive_targets) but were never priced -- they must not be gated.
    assert "2006-01" in payload["predictive_targets"]
    assert payload["inits"] == {"2026-12": "2026-09"}
    assert check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target=payload["inits"], now=SEPT_10,
    ) == []


def test_run_paper_live_target_before_the_expected_month_passes():
    from climate.paper import run_paper

    frame = _mixed_history_frame("2026-08", "2026-08", 1)
    payload = run_paper(frame, _model_leads(1, 2), quotes_df=_quotes_for("2026-08"))

    assert payload["inits"] == {"2026-08": "2026-08"}
    assert check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target=payload["inits"], now=SEPT_10,
    ) == []


def test_run_paper_still_refuses_a_priced_target_with_a_stale_init():
    """Guard catches known-bad: scoping to the ladder must not let a
    genuinely stale PRICED target through. 2026-12's only row is from the
    2026-08 init (a missed September fetch) while an unpriced target sits on
    a fresh init -- the priced one is what the gate must see."""
    from climate.paper import run_paper

    frame = pd.concat([
        _mixed_history_frame("2026-12", "2026-08", 5),
        pd.DataFrame([_row("2027-01", "2026-09", 5)]),
    ], ignore_index=True)
    payload = run_paper(frame, _model_leads(1, 2, 5), quotes_df=_quotes_for("2026-12"))

    assert payload["inits"] == {"2026-12": "2026-08"}
    reasons = check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target=payload["inits"], now=SEPT_10,
    )
    assert len(reasons) == 1
    assert "2026-12" in reasons[0] and "2026-08" in reasons[0]


def test_priced_target_with_no_known_init_is_kept_as_unknown_and_refused():
    """Fail closed: when the frame CAN report inits (it has init_date) but a
    priced target has none, the target is kept with an 'unknown' init (which
    the gate refuses) rather than silently dropped by the ladder scoping."""
    from climate.paper import inits_for_priced_targets

    ladder = pd.DataFrame({"target_month": ["2026-12", "2027-01"]})
    inits = inits_for_priced_targets({"2026-12": "2026-09", "2006-01": "2006-01"}, ladder)
    assert inits == {"2026-12": "2026-09", "2027-01": "unknown"}
    assert len(check_staleness(
        sweep_ts=None, signal_ts=None, inits_by_target=inits, now=SEPT_10,
    )) == 1
    # A frame with no init_date at all still reports "no known inits" (the
    # documented synthetic-frame path), and an empty ladder prices nothing.
    assert inits_for_priced_targets({}, ladder) == {}
    assert inits_for_priced_targets({"2026-12": "2026-09"}, pd.DataFrame()) == {}


def _write_fresh_sweep(ticks_root, ts: datetime) -> None:
    sweep_dir = ticks_root / "quotes" / f"date={ts.strftime('%Y-%m-%d')}"
    sweep_dir.mkdir(parents=True)
    _sweep_rows(ts.isoformat()).to_parquet(
        sweep_dir / ts.strftime("quotes-%H%M%SZ.parquet"), index=False
    )


def _run_gtm_paper_on_mixed_frame(tmp_path, monkeypatch, live_init_offset: int):
    """scripts/run_gtm_paper.py end to end on the reviewer's repro shape: a
    real-looking frame {2006 resolved, live target} plus a FRESH,
    correctly-named sweep on disk. The live target sits 3 months after the
    expected init month; its init is ``expected + live_init_offset``."""
    import climate.paper as paper_mod
    import scripts.run_gtm_paper as run_gtm_paper_mod

    now = datetime.now(timezone.utc)
    expected = expected_latest_init(now.date())
    live_target = _shift_month(expected, 3)
    live_init = _shift_month(expected, live_init_offset)
    lead = 4 - live_init_offset  # target = init + (lead - 1)

    root = tmp_path / "climate_root"
    ticks_root = tmp_path / "ibkr_ticks"
    model_path = tmp_path / "gtm_emos.json"
    _model_leads(1, 2, lead).to_json(model_path)
    _write_fresh_sweep(ticks_root, now - timedelta(minutes=5))
    frame = _mixed_history_frame(live_target, live_init, lead)
    monkeypatch.setattr(run_gtm_paper_mod, "build_training_frame", lambda root: frame)
    monkeypatch.setattr(paper_mod, "latest_gtm_quotes",
                        lambda ticks_root, universe_json=None: _quotes_for(live_target))

    rc = run_gtm_paper_mod.main([
        "--root", str(root),
        "--model", str(model_path),
        "--ticks-root", str(ticks_root),
        "--universe-json", str(tmp_path / "no_universe.json"),
        "--hindcast-gate-only",
    ])
    return rc, root, live_target, expected


def test_run_gtm_paper_writes_signals_for_a_frame_with_resolved_history(
    tmp_path, monkeypatch, capsys
):
    """The reviewer's repro: before the fix this refused with
    'target 2006-01: SEAS5 init 2006-01 is not the expected latest ...'."""
    rc, root, live_target, expected = _run_gtm_paper_on_mixed_frame(
        tmp_path, monkeypatch, live_init_offset=0,
    )
    err = capsys.readouterr().err
    assert "2006-01" not in err
    assert rc == 0
    written = json.loads((root / "paper" / "gtm_signals.json").read_text(encoding="utf-8"))
    assert written["inits"] == {live_target: expected}


def test_run_gtm_paper_still_refuses_a_stale_live_init_on_a_mixed_frame(
    tmp_path, monkeypatch, capsys
):
    rc, root, live_target, expected = _run_gtm_paper_on_mixed_frame(
        tmp_path, monkeypatch, live_init_offset=-1,
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert not (root / "paper" / "gtm_signals.json").exists()
    assert f"target {live_target}" in err and expected in err
    assert "2006-01" not in err  # history is not what refused it


class _GatePassed(Exception):
    """Raised by the fake adapter: reaching IBKRAdapter() means every
    staleness check passed (the gate runs BEFORE the adapter is built)."""


def _run_ibkr_paper_with_inits(tmp_path, monkeypatch, inits: dict) -> int:
    import scripts.run_ibkr_paper as run_ibkr_paper_mod

    class _FakeSettings:
        trading_mode = "paper"
        resolved_data_root = tmp_path / "ibkr_ticks"  # no marker above it: a live root

    class _SentinelAdapter:
        def __init__(self, *args, **kwargs):
            raise _GatePassed

    # Fake settings: no IBKR_* env and no .env read needed.
    monkeypatch.setattr(run_ibkr_paper_mod, "IBKRSettings", _FakeSettings)
    monkeypatch.setattr(run_ibkr_paper_mod, "IBKRAdapter", _SentinelAdapter)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signals_path = tmp_path / "gtm_signals.json"
    signals_path.write_text(json.dumps({
        "ts_utc": now_str, "sweep_ts_utc": now_str, "inits": inits,
        "n_quotes": 0, "n_ladder": 0, "n_tradeable": 0, "predictive_targets": {},
        "go_live": {"passed": False, "reasons": [], "metrics": {}}, "signals": [],
    }), encoding="utf-8")
    return run_ibkr_paper_mod.main(["--signals", str(signals_path), "--simulate"])


def test_run_ibkr_paper_accepts_a_past_target_on_its_own_init_month(tmp_path, monkeypatch):
    expected = expected_latest_init(datetime.now(timezone.utc).date())
    past = _shift_month(expected, -1)
    with pytest.raises(_GatePassed):
        _run_ibkr_paper_with_inits(tmp_path, monkeypatch, {past: past})


def test_run_ibkr_paper_refuses_a_past_target_with_a_stale_init(tmp_path, monkeypatch, capsys):
    expected = expected_latest_init(datetime.now(timezone.utc).date())
    past = _shift_month(expected, -1)
    rc = _run_ibkr_paper_with_inits(tmp_path, monkeypatch, {past: _shift_month(past, -1)})
    assert rc == 1
    assert f"target {past}" in capsys.readouterr().err
