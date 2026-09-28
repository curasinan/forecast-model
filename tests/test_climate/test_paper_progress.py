"""Go-live gate inputs computed from the run/label ledgers (audit finding M2b).

Covers climate/paper_progress.py::compute_paper_progress, the
--paper-days/--resolved-months refusal + computed-default wiring in
scripts/run_gtm_paper.py, and the NaN-coverage strengthening in
climate/paper_criteria.py::evaluate_hindcast_go_live (folds L4: a NaN
coverage row must now fail the gate with a reason instead of silently
passing). No network/Gateway; tmp_path only.

Run-ledger fixtures mirror scripts/run_ibkr_paper.py's append site
key-for-key (ts_utc, model_fingerprint, account, simulated, mode, intents,
executed, rejected, orders[order_id, market_id, outcome, price, size,
status, venue_order_id, perm_id, net_ev, holding_days]) -- a hand-drawn
shape the real traversal never exercises is this team's documented past
failure mode. Label-ledger fixtures go through climate.storage's own
append_observations so they are byte-for-byte what
scripts/collect_climate_labels.py would have written.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from climate.emos import EmosModel, LeadParams
from climate.noaa import LabelObservation
from climate.paper_criteria import ClimateGoLiveCriteria, evaluate_hindcast_go_live
from climate.paper_progress import PaperProgress, compute_paper_progress
from climate.storage import append_observations


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #


def _order(
    *,
    order_id: str = "o1",
    market_id: str = "ibkr:712856754",
    outcome: str = "YES",
    price: float = 0.42,
    size: float = 1.0,
    status: str = "filled",
    venue_order_id: str = "v1",
    perm_id: int = 1001,
    net_ev: float = 0.05,
    holding_days: float = 20.0,
) -> dict:
    """One order dict, key-for-key what run_ibkr_paper.py's append site writes."""
    return {
        "order_id": order_id,
        "market_id": market_id,
        "outcome": outcome,
        "price": price,
        "size": size,
        "status": status,
        "venue_order_id": venue_order_id,
        "perm_id": perm_id,
        "net_ev": net_ev,
        "holding_days": holding_days,
    }


def _run_record(
    *,
    ts_utc: str,
    simulated: bool,
    orders: list[dict] | None = None,
    account: str = "DU12345",
    mode: str = "approve",
    model_fingerprint: str | None = "abc123",
    extra: dict | None = None,
) -> dict:
    """One run-ledger line, key-for-key what run_ibkr_paper.py appends.

    ``extra`` merges in additional keys (e.g. a future field) to prove the
    traversal tolerates keys it doesn't know about.
    """
    orders = orders if orders is not None else []
    record = {
        "ts_utc": ts_utc,
        "model_fingerprint": model_fingerprint,
        "account": account,
        "simulated": simulated,
        "mode": mode,
        "intents": len(orders),
        "executed": len(orders),
        "rejected": 0,
        "orders": orders,
    }
    if extra:
        record.update(extra)
    return record


def _write_runs_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _write_labels_ledger(root: Path, periods: list[str], *, series: str = "gtm") -> None:
    """Append first-print gtm rows for each 'YYYY-MM' period, via the real
    climate.storage writer (same code path scripts/collect_climate_labels.py
    uses)."""
    rows = [
        LabelObservation(series, period, 1.0 + i * 0.01, "https://example/gtm")
        for i, period in enumerate(periods)
    ]
    append_observations(rows, backfill=True, root=root)


# --------------------------------------------------------------------------- #
# compute_paper_progress -- missing / empty / malformed input
# --------------------------------------------------------------------------- #


def test_missing_runs_jsonl_returns_zero_none(tmp_path):
    progress = compute_paper_progress(
        runs_jsonl=tmp_path / "does_not_exist.jsonl",
        labels_root=tmp_path,
        now=datetime(2026, 9, 28, tzinfo=timezone.utc),
    )
    assert progress == PaperProgress(paper_days=0, resolved_months=0, first_order_ts=None)


def test_empty_runs_jsonl_returns_zero_none(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    runs_jsonl.write_text("", encoding="utf-8")
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 9, 28, tzinfo=timezone.utc)
    )
    assert progress == PaperProgress(0, 0, None)


def test_missing_labels_root_yields_zero_resolved_months_but_still_computes_days(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [
            _run_record(
                ts_utc="2026-01-01T00:00:00+00:00",
                simulated=False,
                orders=[_order()],
            )
        ],
    )
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=None, now=datetime(2026, 1, 11, tzinfo=timezone.utc)
    )
    assert progress.paper_days == 10
    assert progress.resolved_months == 0
    assert progress.first_order_ts == "2026-01-01T00:00:00+00:00"


def test_labels_root_without_a_ledger_file_yet_yields_zero_resolved_months(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [_run_record(ts_utc="2026-01-01T00:00:00+00:00", simulated=False, orders=[_order()])],
    )
    labels_root = tmp_path / "no_labels_yet"
    labels_root.mkdir()
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=labels_root, now=datetime(2026, 1, 11, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 0


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    good = _run_record(ts_utc="2026-01-01T00:00:00+00:00", simulated=False, orders=[_order()])
    runs_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with runs_jsonl.open("w", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write("\n")  # blank line
        fh.write(json.dumps(good) + "\n")
        fh.write("[1, 2, 3]\n")  # valid JSON, not an object
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=None, now=datetime(2026, 1, 5, tzinfo=timezone.utc)
    )
    assert progress.first_order_ts == "2026-01-01T00:00:00+00:00"
    assert progress.paper_days == 4


# --------------------------------------------------------------------------- #
# paper_days: first simulated=false line, floored day-rounding, clamped
# --------------------------------------------------------------------------- #


def test_paper_days_ignores_leading_simulated_runs(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [
            _run_record(ts_utc="2025-12-01T00:00:00+00:00", simulated=True, orders=[_order()]),
            _run_record(ts_utc="2026-01-01T00:00:00+00:00", simulated=False, orders=[_order()]),
            _run_record(ts_utc="2026-01-05T00:00:00+00:00", simulated=False, orders=[_order()]),
        ],
    )
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=None, now=datetime(2026, 1, 11, tzinfo=timezone.utc)
    )
    # First REAL (simulated=false) line wins, not the earlier simulated one
    # and not a later real one.
    assert progress.first_order_ts == "2026-01-01T00:00:00+00:00"
    assert progress.paper_days == 10


@pytest.mark.parametrize(
    "first_ts,now,expected_days",
    [
        # exactly 1 day 23:59:59 elapsed -> floors to 1, not 2
        ("2026-01-01T00:00:00+00:00", datetime(2026, 1, 2, 23, 59, 59, tzinfo=timezone.utc), 1),
        # exactly 10 days elapsed -> 10
        ("2026-01-01T00:00:00+00:00", datetime(2026, 1, 11, 0, 0, 0, tzinfo=timezone.utc), 10),
        # under 1 day elapsed -> 0, never rounds up
        ("2026-01-01T12:00:00+00:00", datetime(2026, 1, 2, 11, 59, 59, tzinfo=timezone.utc), 0),
    ],
)
def test_paper_days_floors_rather_than_rounds(tmp_path, first_ts, now, expected_days):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl, [_run_record(ts_utc=first_ts, simulated=False, orders=[_order()])]
    )
    progress = compute_paper_progress(runs_jsonl=runs_jsonl, labels_root=None, now=now)
    assert progress.paper_days == expected_days


def test_paper_days_clamps_negative_to_zero(tmp_path):
    """Clock skewed behind the first order's own timestamp must never
    report a negative paper-days count."""
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [_run_record(ts_utc="2026-06-01T00:00:00+00:00", simulated=False, orders=[_order()])],
    )
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=None, now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert progress.paper_days == 0


def test_paper_days_zero_and_first_order_ts_none_when_no_real_run_yet(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [_run_record(ts_utc="2026-01-01T00:00:00+00:00", simulated=True, orders=[_order()])],
    )
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=None, now=datetime(2026, 6, 1, tzinfo=timezone.utc)
    )
    assert progress.paper_days == 0
    assert progress.first_order_ts is None


# --------------------------------------------------------------------------- #
# resolved_months: distinct traded months with a first-print gtm label
# --------------------------------------------------------------------------- #


def test_resolved_months_counts_distinct_traded_months_with_a_first_print_label(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [
            _run_record(ts_utc="2026-01-15T00:00:00+00:00", simulated=False, orders=[_order()]),
            _run_record(ts_utc="2026-02-15T00:00:00+00:00", simulated=False, orders=[_order()]),
        ],
    )
    _write_labels_ledger(tmp_path, ["2026-01", "2026-02"])
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 2


def test_resolved_months_excludes_traded_months_without_a_label(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [
            _run_record(ts_utc="2026-01-15T00:00:00+00:00", simulated=False, orders=[_order()]),
            _run_record(ts_utc="2026-02-15T00:00:00+00:00", simulated=False, orders=[_order()]),
        ],
    )
    _write_labels_ledger(tmp_path, ["2026-01"])  # 2026-02 not yet published
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 1


def test_resolved_months_excludes_labels_without_a_traded_month(tmp_path):
    """A published label for a month we never traded doesn't count."""
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [_run_record(ts_utc="2026-01-15T00:00:00+00:00", simulated=False, orders=[_order()])],
    )
    _write_labels_ledger(tmp_path, ["2026-01", "2025-06"])
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 1


def test_resolved_months_requires_a_non_empty_orders_list(tmp_path):
    """A run with zero orders (e.g. nothing tradeable that day) does not
    count as having 'traded' that month, even if the month later resolves."""
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [_run_record(ts_utc="2026-01-15T00:00:00+00:00", simulated=False, orders=[])],
    )
    _write_labels_ledger(tmp_path, ["2026-01"])
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 0


def test_resolved_months_dedupes_multiple_runs_in_the_same_month(tmp_path):
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [
            _run_record(ts_utc="2026-01-05T00:00:00+00:00", simulated=False, orders=[_order()]),
            _run_record(ts_utc="2026-01-20T00:00:00+00:00", simulated=False, orders=[_order()]),
        ],
    )
    _write_labels_ledger(tmp_path, ["2026-01"])
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 1


def test_resolved_months_counts_simulated_runs_orders_too(tmp_path):
    """Unlike paper_days, resolved_months is scoped to 'any run's orders'
    per the brief -- a simulated/rehearsal run's traded month still counts
    here. This alone cannot inflate the overall go-live gate: paper_days
    stays 0 without a real (simulated=false) run, and evaluate_hindcast_go_live
    requires BOTH thresholds to clear."""
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [_run_record(ts_utc="2026-01-15T00:00:00+00:00", simulated=True, orders=[_order()])],
    )
    _write_labels_ledger(tmp_path, ["2026-01"])
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 1
    assert progress.paper_days == 0  # no real run -> the clock never started


def test_resolved_months_ignores_a_revision_row_without_a_first_print(tmp_path):
    """latest_values()/first-print discipline: is_revision=True rows never
    stand in for a missing first-print row (Rule 603(e))."""
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [_run_record(ts_utc="2026-01-15T00:00:00+00:00", simulated=False, orders=[_order()])],
    )
    # Manually seed a ledger with ONLY a revision row (contrived -- the real
    # writer never does this, but the traversal must not be fooled by it).
    # Written via the ledger's own public path/columns constants, straight
    # to parquet, rather than climate.storage's private write helper.
    from climate.storage import LEDGER_COLUMNS, ledger_path

    df = pd.DataFrame(
        [
            {
                "series": "gtm",
                "period": "2026-01",
                "value": 1.23,
                "first_seen_utc": "2026-02-01T00:00:00+00:00",
                "vintage": "live_first_print",
                "is_revision": True,
                "source_url": "https://example/gtm",
            }
        ],
        columns=LEDGER_COLUMNS,
    )
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 0


def test_extra_unknown_keys_on_a_run_record_are_tolerated(tmp_path):
    """A run record may carry keys this function doesn't know about (e.g. a
    future field) without breaking the traversal."""
    runs_jsonl = tmp_path / "ibkr_paper_runs.jsonl"
    _write_runs_jsonl(
        runs_jsonl,
        [
            _run_record(
                ts_utc="2026-01-15T00:00:00+00:00",
                simulated=False,
                orders=[_order()],
                extra={"some_future_field": {"nested": True}, "audit_note": "ok"},
            )
        ],
    )
    _write_labels_ledger(tmp_path, ["2026-01"])
    progress = compute_paper_progress(
        runs_jsonl=runs_jsonl, labels_root=tmp_path, now=datetime(2026, 3, 1, tzinfo=timezone.utc)
    )
    assert progress.resolved_months == 1
    assert progress.first_order_ts == "2026-01-15T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# scripts/run_gtm_paper.py -- computed defaults + --allow-manual-gate-inputs
# --------------------------------------------------------------------------- #


def _minimal_payload(**overrides) -> dict:
    payload = {
        "ts_utc": "2026-01-01T00:00:00Z",
        "sweep_ts_utc": None,
        "model_fingerprint": "x",
        "inits": {},
        "n_quotes": 0,
        "n_ladder": 0,
        "n_tradeable": 0,
        "predictive_targets": {},
        "go_live": {"passed": False, "reasons": [], "metrics": {}},
        "signals": [],
    }
    payload.update(overrides)
    return payload


def test_run_gtm_paper_computes_default_gate_inputs_from_the_ledgers(tmp_path, monkeypatch):
    import scripts.run_gtm_paper as run_gtm_paper_mod

    root = tmp_path / "climate_root"
    model_path = tmp_path / "gtm_emos.json"
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(lead_month=1, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1)
    model.to_json(model_path)

    test_now = datetime.now(timezone.utc)
    first_ts = test_now - timedelta(days=10, hours=12)  # safely floors to 10
    _write_runs_jsonl(
        root / "paper" / "ibkr_paper_runs.jsonl",
        [_run_record(ts_utc=first_ts.isoformat(), simulated=False, orders=[_order()])],
    )
    _write_labels_ledger(root, [f"{first_ts.year:04d}-{first_ts.month:02d}"])

    captured: dict = {}

    def _fake_run_paper(frame, model_, **kwargs):
        captured.update(kwargs)
        return _minimal_payload()

    monkeypatch.setattr(run_gtm_paper_mod, "run_paper", _fake_run_paper)
    monkeypatch.setattr(run_gtm_paper_mod, "build_training_frame", lambda r: pd.DataFrame())

    run_gtm_paper_mod.main(
        [
            "--root", str(root),
            "--model", str(model_path),
            "--ticks-root", str(tmp_path / "ticks"),
            "--universe-json", str(tmp_path / "no_universe.json"),
        ]
    )

    assert captured["paper_days"] == 10
    assert captured["resolved_months"] == 1


def test_run_gtm_paper_refuses_manual_paper_days_without_the_flag(tmp_path, monkeypatch, capsys):
    import scripts.run_gtm_paper as run_gtm_paper_mod

    root = tmp_path / "climate_root"
    model_path = tmp_path / "gtm_emos.json"
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(lead_month=1, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1)
    model.to_json(model_path)

    def _raising_run_paper(*args, **kwargs):
        raise AssertionError("run_paper must never be called on a refused manual override")

    monkeypatch.setattr(run_gtm_paper_mod, "run_paper", _raising_run_paper)
    monkeypatch.setattr(run_gtm_paper_mod, "build_training_frame", lambda r: pd.DataFrame())

    rc = run_gtm_paper_mod.main(
        [
            "--root", str(root),
            "--model", str(model_path),
            "--ticks-root", str(tmp_path / "ticks"),
            "--universe-json", str(tmp_path / "no_universe.json"),
            "--paper-days", "999",
        ]
    )

    assert rc != 0
    captured = capsys.readouterr()
    assert "--allow-manual-gate-inputs" in captured.err


def test_run_gtm_paper_refuses_manual_resolved_months_without_the_flag(tmp_path, monkeypatch, capsys):
    import scripts.run_gtm_paper as run_gtm_paper_mod

    root = tmp_path / "climate_root"
    model_path = tmp_path / "gtm_emos.json"
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(lead_month=1, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1)
    model.to_json(model_path)

    def _raising_run_paper(*args, **kwargs):
        raise AssertionError("run_paper must never be called on a refused manual override")

    monkeypatch.setattr(run_gtm_paper_mod, "run_paper", _raising_run_paper)
    monkeypatch.setattr(run_gtm_paper_mod, "build_training_frame", lambda r: pd.DataFrame())

    rc = run_gtm_paper_mod.main(
        [
            "--root", str(root),
            "--model", str(model_path),
            "--ticks-root", str(tmp_path / "ticks"),
            "--universe-json", str(tmp_path / "no_universe.json"),
            "--resolved-months", "12",
        ]
    )

    assert rc != 0
    captured = capsys.readouterr()
    assert "--allow-manual-gate-inputs" in captured.err


def test_run_gtm_paper_allows_manual_gate_inputs_with_the_flag(tmp_path, monkeypatch):
    import scripts.run_gtm_paper as run_gtm_paper_mod

    root = tmp_path / "climate_root"
    model_path = tmp_path / "gtm_emos.json"
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(lead_month=1, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1)
    model.to_json(model_path)

    captured: dict = {}

    def _fake_run_paper(frame, model_, **kwargs):
        captured.update(kwargs)
        return _minimal_payload()

    monkeypatch.setattr(run_gtm_paper_mod, "run_paper", _fake_run_paper)
    monkeypatch.setattr(run_gtm_paper_mod, "build_training_frame", lambda r: pd.DataFrame())

    run_gtm_paper_mod.main(
        [
            "--root", str(root),
            "--model", str(model_path),
            "--ticks-root", str(tmp_path / "ticks"),
            "--universe-json", str(tmp_path / "no_universe.json"),
            "--paper-days", "999",
            "--resolved-months", "5",
            "--allow-manual-gate-inputs",
        ]
    )

    assert captured["paper_days"] == 999
    assert captured["resolved_months"] == 5


# --------------------------------------------------------------------------- #
# NaN coverage strengthening (L4): fails the gate with a reason instead of
# passing silently.
# --------------------------------------------------------------------------- #


def test_nan_cover50_now_fails_with_a_reason_instead_of_silently_passing():
    summary = pd.DataFrame(
        {
            "lead_month": [1, 2, 3],
            "n": [80, 80, 80],
            "skill_vs_trend": [0.05, 0.04, 0.03],
            "cover50_emos": [np.nan, 0.51, 0.50],
            "cover90_emos": [0.88, 0.91, 0.90],
        }
    )
    verdict = evaluate_hindcast_go_live(summary, require_live_paper=False)
    assert not verdict.passed
    assert any("cover50" in r and "1" in r for r in verdict.reasons)


def test_nan_cover90_now_fails_with_a_reason_instead_of_silently_passing():
    summary = pd.DataFrame(
        {
            "lead_month": [1, 2, 3],
            "n": [80, 80, 80],
            "skill_vs_trend": [0.05, 0.04, 0.03],
            "cover50_emos": [0.48, 0.51, 0.50],
            "cover90_emos": [0.88, np.nan, 0.90],
        }
    )
    verdict = evaluate_hindcast_go_live(summary, require_live_paper=False)
    assert not verdict.passed
    assert any("cover90" in r and "2" in r for r in verdict.reasons)


def test_finite_in_band_coverage_still_passes_byte_identical(tmp_path):
    """Regress note: passing behavior on finite, in-band inputs must stay
    byte-identical -- only the NaN path is new."""
    summary = pd.DataFrame(
        {
            "lead_month": [1, 2, 3],
            "n": [80, 80, 80],
            "skill_vs_trend": [0.05, 0.04, 0.03],
            "cover50_emos": [0.48, 0.51, 0.50],
            "cover90_emos": [0.88, 0.91, 0.90],
        }
    )
    verdict = evaluate_hindcast_go_live(summary, require_live_paper=False)
    assert verdict.passed
    assert verdict.reasons == []


def test_min_leads_beating_trend_comment_describes_any_three_leads():
    """The old comment said 'leads 1..3 must clear the skill floor', which
    is not what the code does (it counts ANY N leads beating trend,
    regardless of which lead numbers). Pin the corrected wording so a
    future edit can't silently restore the wrong claim."""
    import inspect

    source = inspect.getsource(ClimateGoLiveCriteria)
    assert "any 3 leads" in source
