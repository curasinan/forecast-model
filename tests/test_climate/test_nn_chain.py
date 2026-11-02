"""The nowcast chain: a strict benchmark run's saved network priced again by the payload script."""

from __future__ import annotations

import json

import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from scripts.research.gtm_nowcast import main as nowcast_main  # noqa: E402
from scripts.research.gtm_nowcast_benchmark import main as benchmark_main  # noqa: E402

TARGET, CHECKPOINT, AS_OF = "2011-06", "20", "2011-06-20T00:00:00Z"  # a decision of the last year


def test_the_payload_of_a_saved_network_is_its_row_in_the_run(tmp_path, nowcast_root):
    run_dir = tmp_path / "run"
    argv = ["--root", str(nowcast_root), "--run-dir", str(run_dir), "--models", "nn,bridge"]
    argv += ["--seeds", "0", "--hidden", "8"]
    argv += ["--first-test-year", "2011", "--last-test-year", "2011"]
    assert benchmark_main(argv) == 0
    out = tmp_path / "payload.json"
    argv = ["--root", str(nowcast_root), "--model-dir", str(run_dir / "weights")]
    argv += ["--as-of", AS_OF, "--target-month", TARGET, "--out", str(out)]
    assert nowcast_main(argv) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["usable"] is True and payload["checkpoint"] == CHECKPOINT
    rows = pd.read_parquet(run_dir / "rows.parquet")
    at = (rows["target_month"] == TARGET) & (rows["checkpoint"] == CHECKPOINT)
    (row,) = rows[at].itertuples()
    assert row.test_year == 2011
    assert payload["mu"] == pytest.approx(row.mu_nn, abs=1e-6)
    assert payload["sigma"] == pytest.approx(row.sigma_nn, abs=1e-6)
