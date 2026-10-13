"""Climate label pipeline: parsers, first-print ledger, collector. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from climate.noaa import (
    GML_CO2_ANNUAL_URL,
    GML_CO2_MONTHLY_URL,
    LabelObservation,
    OracleDriftError,
    parse_cag_json,
    parse_gml_annual,
    parse_gml_monthly,
)
from climate.storage import (
    VINTAGE_BACKFILL,
    VINTAGE_LIVE,
    append_observations,
    latest_values,
    ledger_path,
    load_ledger,
    revision_history,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import collect_climate_labels  # noqa: E402

CAG_URL = "https://example.test/data.json"

GTM_JSON = {
    "description": {
        "title": "Global Land and Ocean Average Temperature Departures",
        "units": "Degrees Celsius",
        "base_period": "1901-2000",
    },
    "data": {
        "185002": {"departure": -0.13},
        "185001": {"departure": -0.43},
        "202607": {"departure": 1.18},
    },
}

GT_JSON = {
    "description": {
        "title": "Global Land and Ocean January - December Average Temperature Departures",
        "units": "Degrees Celsius",
        "base_period": "1901-2000",
    },
    "data": {
        "1850": {"departure": -0.15},
        "2025": {"departure": 1.13},
    },
}

ANN_TXT = """\
# --------------------------------------------------------------------
# USE OF NOAA GML DATA
#
# year     mean      unc
  1959   315.98     0.12
  1960   316.91     0.12
  2025   427.35     0.12
"""

MM_TXT = """\
# --------------------------------------------------------------------
#            decimal       monthly    de-season  #days  st.dev  unc. of
#             date         average     alized          of days  mon mean
 1958    3   1958.2027      315.71      314.44     -1   -9.99   -0.99
 1958    6   1958.4548       -9.99      314.85     -1   -9.99   -0.99
 2026    7   2026.5417      429.12      428.83     21    0.59    0.25
"""


def test_parse_gtm_json():
    rows = parse_cag_json(GTM_JSON, "gtm", "monthly", CAG_URL)
    assert rows == [
        LabelObservation("gtm", "1850-01", -0.43, CAG_URL),
        LabelObservation("gtm", "1850-02", -0.13, CAG_URL),
        LabelObservation("gtm", "2026-07", 1.18, CAG_URL),
    ]


def test_parse_gt_json():
    rows = parse_cag_json(GT_JSON, "gt", "annual", CAG_URL)
    assert rows == [
        LabelObservation("gt", "1850", -0.15, CAG_URL),
        LabelObservation("gt", "2025", 1.13, CAG_URL),
    ]


def test_base_period_drift_fails_loudly():
    bad = {
        "description": {"units": "Degrees Celsius", "base_period": "1991-2020"},
        "data": {"185001": {"departure": -0.43}},
    }
    with pytest.raises(OracleDriftError, match="1901-2000"):
        parse_cag_json(bad, "gtm", "monthly", CAG_URL)


def test_units_drift_fails_loudly():
    bad = {
        "description": {"units": "Degrees Fahrenheit", "base_period": "1901-2000"},
        "data": {"185001": {"departure": -0.77}},
    }
    with pytest.raises(OracleDriftError, match="Units"):
        parse_cag_json(bad, "gtm", "monthly", CAG_URL)


def test_parse_gml_annual():
    rows = parse_gml_annual(ANN_TXT, GML_CO2_ANNUAL_URL)
    assert [r.period for r in rows] == ["1959", "1960", "2025"]
    assert rows[0] == LabelObservation("acd_annual", "1959", 315.98, GML_CO2_ANNUAL_URL)
    assert rows[-1].value == 427.35


def test_parse_gml_monthly_uses_average_and_skips_sentinels():
    rows = parse_gml_monthly(MM_TXT, GML_CO2_MONTHLY_URL)
    # 1958-06 has average=-9.99 (missing sentinel) and must be dropped.
    assert [r.period for r in rows] == ["1958-03", "2026-07"]
    assert rows[0].value == 315.71  # 'average' column, not deseasonalized
    assert rows[1] == LabelObservation("acd_monthly", "2026-07", 429.12, GML_CO2_MONTHLY_URL)


def _obs(series="gtm", period="2026-07", value=1.18, url=CAG_URL):
    return LabelObservation(series, period, value, url)


def test_ledger_backfill_then_incremental_first_print(tmp_path):
    root = tmp_path
    summary = append_observations([_obs(period="2026-06", value=1.11), _obs()], True, root=root)
    assert summary == {"gtm": {"new": 2, "revisions": 0, "skipped": 0}}
    ledger = load_ledger(root)
    assert list(ledger["vintage"].unique()) == [VINTAGE_BACKFILL]
    assert not ledger["is_revision"].any()

    # New period on a live run -> live_first_print.
    summary = append_observations([_obs(period="2026-08", value=1.02)], False, root=root)
    assert summary == {"gtm": {"new": 1, "revisions": 0, "skipped": 0}}
    ledger = load_ledger(root)
    row = ledger[ledger["period"] == "2026-08"].iloc[0]
    assert row["vintage"] == VINTAGE_LIVE
    assert row["source_url"] == CAG_URL
    assert latest_values("gtm", root=root) == {
        "2026-06": 1.11,
        "2026-07": 1.18,
        "2026-08": 1.02,
    }


def test_ledger_idempotent_reruns(tmp_path):
    root = tmp_path
    rows = [_obs(period="2026-06", value=1.11), _obs()]
    append_observations(rows, True, root=root)
    n_before = len(load_ledger(root))
    summary = append_observations(rows, False, root=root)
    assert summary == {"gtm": {"new": 0, "revisions": 0, "skipped": 2}}
    assert len(load_ledger(root)) == n_before


def test_ledger_revision_never_overwrites_first_print(tmp_path):
    root = tmp_path
    append_observations([_obs(value=1.18)], True, root=root)
    summary = append_observations([_obs(value=1.20)], False, root=root)
    assert summary == {"gtm": {"new": 0, "revisions": 1, "skipped": 0}}

    ledger = load_ledger(root)
    assert len(ledger) == 2
    first = ledger.iloc[0]
    assert first["value"] == 1.18 and not first["is_revision"]

    # Settlement view: first print wins over the revision.
    assert latest_values("gtm", root=root) == {"2026-07": 1.18}
    revs = revision_history("gtm", root=root)
    assert len(revs) == 1
    assert revs.iloc[0]["value"] == 1.20 and bool(revs.iloc[0]["is_revision"])

    # Re-fetching the already-recorded revision value appends nothing.
    summary = append_observations([_obs(value=1.20)], False, root=root)
    assert summary == {"gtm": {"new": 0, "revisions": 0, "skipped": 1}}
    assert len(load_ledger(root)) == 2

    # A further revision (back to a third value) is a new revision row.
    summary = append_observations([_obs(value=1.19)], False, root=root)
    assert summary == {"gtm": {"new": 0, "revisions": 1, "skipped": 0}}
    assert len(revision_history("gtm", root=root)) == 2
    assert latest_values("gtm", root=root) == {"2026-07": 1.18}


def test_ledger_multiple_series_partitioned(tmp_path):
    root = tmp_path
    append_observations(
        [_obs(), _obs(series="acd_annual", period="2025", value=427.35)], True, root=root
    )
    assert latest_values("gtm", root=root) == {"2026-07": 1.18}
    assert latest_values("acd_annual", root=root) == {"2025": 427.35}
    assert latest_values("gt", root=root) == {}


def _fake_fetchers(gtm_rows=None):
    return {
        "gtm": lambda: gtm_rows
        or [
            _obs(period="2026-06", value=1.11),
            _obs(period="2026-07", value=1.18),
        ],
        "gt": lambda: [_obs(series="gt", period="2025", value=1.13)],
        "acd_annual": lambda: [_obs(series="acd_annual", period="2025", value=427.35)],
        "acd_monthly": lambda: [_obs(series="acd_monthly", period="2026-07", value=429.12)],
    }


def _run(monkeypatch, tmp_path, argv, fetchers):
    monkeypatch.setattr(collect_climate_labels, "FETCHERS", fetchers)
    return collect_climate_labels.main([*argv, "--data-root", str(tmp_path)])


def test_collector_backfill_then_noop(monkeypatch, tmp_path):
    fetchers = _fake_fetchers()
    assert _run(monkeypatch, tmp_path, ["--backfill"], fetchers) == 0
    ledger = load_ledger(tmp_path)
    assert len(ledger) == 5
    assert set(ledger["vintage"]) == {VINTAGE_BACKFILL}

    # Second incremental run with identical data: exit 0, nothing appended.
    assert _run(monkeypatch, tmp_path, [], fetchers) == 0
    assert len(load_ledger(tmp_path)) == 5


def test_collector_incremental_new_and_revision(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, ["--backfill"], _fake_fetchers()) == 0

    updated = _fake_fetchers(
        gtm_rows=[
            _obs(period="2026-06", value=1.11),  # unchanged -> skip
            _obs(period="2026-07", value=1.20),  # revised -> revision row
            _obs(period="2026-08", value=1.02),  # new -> live_first_print
        ]
    )
    assert _run(monkeypatch, tmp_path, [], updated) == 0
    ledger = load_ledger(tmp_path)
    assert len(ledger) == 7
    new_row = ledger[(ledger["period"] == "2026-08") & (ledger["series"] == "gtm")].iloc[0]
    assert new_row["vintage"] == VINTAGE_LIVE and not new_row["is_revision"]
    assert latest_values("gtm", root=tmp_path)["2026-07"] == 1.18  # first print wins
    assert len(revision_history("gtm", root=tmp_path)) == 1


def test_collector_exits_1_on_any_fetch_failure(monkeypatch, tmp_path):
    fetchers = _fake_fetchers()

    def boom():
        raise RuntimeError("NOAA down")

    fetchers["acd_monthly"] = boom
    assert _run(monkeypatch, tmp_path, ["--backfill"], fetchers) == 1
    # Successful series were still appended before the failure exit.
    ledger = load_ledger(tmp_path)
    assert set(ledger["series"]) == {"gtm", "gt", "acd_annual"}


def test_ledger_path_under_root(tmp_path):
    assert ledger_path(tmp_path) == tmp_path / "labels" / "labels_ledger.parquet"
