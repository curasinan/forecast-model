"""S1 model-vs-market comparison and the two simulation CLIs."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from climate.emos import crps_gaussian
from climate.sim.compare import compare_snapshots, first_prints, print_level_summary
from climate.sim.ladders import ladder_frame, read_payloads
from climate.storage import VINTAGE_BACKFILL, VINTAGE_LIVE
from scripts.research import gtm_sim_book, gtm_sim_score
from tests.test_climate.sim_fixtures import ladder_quotes, payload

STRIKES = np.round(np.arange(-0.2, 1.71, 0.1), 2)


def _payloads():
    dec = ladder_quotes(STRIKES, 1.30, 0.18, ceiling=0.93)
    sep = ladder_quotes(np.round(np.arange(0.9, 1.71, 0.1), 2), 1.33, 0.10)
    dead = [(k, 0.01, 0.99) for k in STRIKES]
    first = payload("2026-09-28T22:08:22Z", {"2026-09": sep, "2026-12": dec, "2026-10": dead},
                    {"2026-09": (1.36, 0.08), "2026-12": (1.44, 0.14), "2026-10": (1.42, 0.09)})
    second = payload("2026-10-16T13:31:46Z", {"2026-12": dec}, {"2026-12": (1.43, 0.12)})
    return [first, second]


def _ledger(rows):
    return pd.DataFrame(rows, columns=["series", "period", "value", "first_seen_utc", "vintage",
                                       "is_revision", "source_url"])


def test_first_prints_prefer_the_live_first_print_and_skip_revisions():
    ledger = _ledger([
        ("gtm", "2026-08", 1.25, "t0", VINTAGE_BACKFILL, False, "u"),
        ("gtm", "2026-08", 1.27, "t1", VINTAGE_LIVE, False, "u"),
        ("gtm", "2026-08", 1.29, "t2", VINTAGE_LIVE, True, "u"),
        ("gtm", "2025-12", 1.20, "t0", VINTAGE_BACKFILL, False, "u"),
        ("gt", "2025", 1.10, "t0", VINTAGE_BACKFILL, False, "u"),
    ])
    out = first_prints(ledger)
    assert out == {"2026-08": (1.27, VINTAGE_LIVE), "2025-12": (1.20, VINTAGE_BACKFILL)}


def test_compare_lines_up_model_and_market_per_snapshot():
    ladders = ladder_frame(_payloads())
    out = compare_snapshots(ladders)
    assert list(zip(out["snapshot_utc"].dt.strftime("%m-%d"), out["target_month"])) == [
        ("09-28", "2026-09"), ("09-28", "2026-12"), ("10-16", "2026-12")]  # October has no market
    sep = out.iloc[0]
    assert sep["running_month"] and sep["lead_months"] == 0
    dec = out.iloc[2]
    assert dec["lead_months"] == 2 and not dec["running_month"]
    assert dec["market_mu"] == pytest.approx(1.30, abs=0.02)
    assert dec["mu_gap"] == pytest.approx(1.43 - dec["market_mu"])
    assert np.isnan(dec["crps_model"])


def test_compare_scores_both_sides_once_the_month_has_printed():
    out = compare_snapshots(ladder_frame(_payloads()), {"2026-12": (1.35, VINTAGE_LIVE)})
    dec = out[out["target_month"] == "2026-12"].iloc[-1]
    assert dec["crps_model"] == pytest.approx(float(crps_gaussian(1.35, 1.43, 0.12)))
    assert dec["crps_market"] == pytest.approx(
        float(crps_gaussian(1.35, dec["market_mu"], dec["market_sigma"])))
    assert dec["crps_diff"] == pytest.approx(dec["crps_model"] - dec["crps_market"])
    assert dec["n_brier"] > 0 and dec["label_vintage"] == VINTAGE_LIVE
    summary = print_level_summary(out)
    assert summary["target_month"].tolist() == ["2026-12"]
    assert summary["n_snapshots"].iloc[0] == 2


def test_read_payloads_takes_json_and_jsonl(tmp_path):
    a, b = _payloads()
    (tmp_path / "one.json").write_text(json.dumps(a))
    (tmp_path / "hist.jsonl").write_text(json.dumps(a) + "\n\n" + json.dumps(b) + "\n")
    assert len(read_payloads([tmp_path / "one.json", tmp_path / "hist.jsonl"])) == 3
    (tmp_path / "bad.jsonl").write_text("{not json}\n")
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        read_payloads([tmp_path / "bad.jsonl"])


def test_score_cli_writes_a_run_directory(tmp_path):
    a, b = _payloads()
    hist = tmp_path / "hist.jsonl"
    hist.write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    out = tmp_path / "run"
    assert gtm_sim_score.main(["--payload", str(hist), "--no-labels", "--out", str(out)]) == 0
    assert {p.name for p in out.iterdir()} >= {"config.json", "metrics.json", "rows.parquet", "report.md"}
    assert "nothing is scored yet" in (out / "report.md").read_text()


def test_score_cli_refuses_a_missing_payload(tmp_path):
    assert gtm_sim_score.main(["--payload", str(tmp_path / "nope.json"), "--no-labels",
                               "--out", str(tmp_path / "run")]) == 2


def test_book_cli_values_a_book_under_every_scenario(tmp_path):
    a, b = _payloads()
    path = tmp_path / "p.json"
    path.write_text(json.dumps(b))
    out = tmp_path / "run"
    code = gtm_sim_book.main(["--payload", str(path), "--target", "2026-12",
                              "--book", "cap15=1.3:YES:22:0.44,1.2:YES:8:0.65",
                              "--model", "newer=1.425:0.115", "--out", str(out)])
    assert code == 0
    rows = pd.read_parquet(out / "rows.parquet")
    assert rows["scenario"].iloc[0] == "newer"  # a given model leads
    assert {"market", "blend_w0.5", "model_init_2026-10"} <= set(rows["scenario"])


@pytest.mark.parametrize("bad", [["--book", "cap15"], ["--book", "x=1.3:YES:0:0.4"],
                                 ["--book", "x=1.3:YES:2:0.4", "--model", "m=1.4"]])
def test_book_cli_usage_errors_exit_2(tmp_path, bad):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(_payloads()[1]))
    args = ["--payload", str(path), "--target", "2026-12", *bad, "--out", str(tmp_path / "run")]
    assert gtm_sim_book.main(args) == 2


def test_book_cli_refuses_an_unknown_target(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(_payloads()[1]))
    assert gtm_sim_book.main(["--payload", str(path), "--target", "2027-03",
                              "--book", "x=1.3:YES:2:0.4", "--out", str(tmp_path / "run")]) == 2
