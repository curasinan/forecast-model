"""scripts/fit_gtm_emos.py main(): the root a fit writes under is always named.

A fit overwrites models/gtm_emos.json and the diagnostics of the root it runs on, and the
default root is the live one the paper runtime reads. So every test here runs with that
default redirected: CLIMATE_DATA_ROOT points at a synthetic root under tmp_path that holds
a stand-in for the operational model, and the home directory behind the variable (HOME /
USERPROFILE) at an empty directory there. A guard that does not hold refits the synthetic
root and fails the test; it cannot reach the operator's ~/quant_data.

The root has the on-disk layout of test_build_training_frame_from_disk in
test_dataset_bridge.py, filled with the synthetic series of test_fit.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from climate.noaa import LabelObservation
from climate.paths import data_root
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET
from climate.storage import append_observations
from scripts.fit_gtm_emos import main
from tests.test_climate.test_fit import _synthetic_inputs

MODEL = Path("models") / "gtm_emos.json"
REPORT_MD = Path("reports") / "gtm_emos_diagnostics.md"
REPORT_JSON = Path("reports") / "gtm_emos_diagnostics.json"
OPERATIONAL = '{"stand-in": "the operational model, as the paper runtime last read it"}'


def _write_fit_inputs(root: Path) -> Path:
    """SEAS5, ERA5 and the label ledger under ``root``: everything a fit reads."""
    seas5, era5, noaa = _synthetic_inputs()
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
    return root


def _exit_status(argv: list[str]) -> int:
    """The status the process would exit with, whether main() returns it or argparse raises it."""
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code


@pytest.fixture(autouse=True)
def live_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The default climate data root: a fittable synthetic root that already holds a model.

    Fittable on purpose. A run that slips past the guard then succeeds, overwrites the
    stand-in model and fails the test on its assertions, instead of dying on a missing
    input and looking like a refusal.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    root = _write_fit_inputs(tmp_path / "live")
    monkeypatch.setenv("CLIMATE_DATA_ROOT", str(root))
    (root / MODEL).parent.mkdir()
    (root / MODEL).write_text(OPERATIONAL, encoding="utf-8")
    return root


def test_fit_without_root_or_live_exits_2_naming_the_files_it_would_have_overwritten(
    live_root, capsys
):
    """Kills: a bare run refitting the default root (the old behaviour), a refusal that does
    not say which files were at stake, which of them exist, or how to go on."""
    status = _exit_status([])
    captured = capsys.readouterr()

    assert status == 2
    lines = captured.err.splitlines()
    (model_line,) = [line for line in lines if str(live_root / MODEL) in line]
    (md_line,) = [line for line in lines if str(live_root / REPORT_MD) in line]
    (json_line,) = [line for line in lines if str(live_root / REPORT_JSON) in line]
    assert "(exists)" in model_line  # the stand-in is there: this one would have been lost
    assert "(exists)" not in md_line and "(exists)" not in json_line
    assert "--root" in lines[-1] and "--live" in lines[-1]  # it ends on the two ways to go on
    # Nothing was fitted or written: the model is byte-for-byte the stand-in, no report came.
    assert (live_root / MODEL).read_text(encoding="utf-8") == OPERATIONAL
    assert not (live_root / "reports").exists()


def test_fit_with_root_fits_that_root_and_leaves_the_default_root_alone(live_root, tmp_path):
    """Kills: a guard that refuses a named root too, and a --root run whose writes land under
    the default root (the comparison refit the Spark runbook documents as routine)."""
    snapshot = _write_fit_inputs(tmp_path / "snapshot")

    rc = main(["--root", str(snapshot)])

    assert rc == 0
    model = json.loads((snapshot / MODEL).read_text(encoding="utf-8"))
    assert sorted(model["leads"]) == ["1", "2", "3"]  # the synthetic series carry leads 1-3
    assert (snapshot / REPORT_MD).exists()
    report = json.loads((snapshot / REPORT_JSON).read_text(encoding="utf-8"))
    assert report["artifact_paths"]["model"] == str(snapshot / MODEL)
    assert (live_root / MODEL).read_text(encoding="utf-8") == OPERATIONAL
    assert not (live_root / "reports").exists()


def test_fit_with_live_refits_the_default_root(live_root):
    """Kills: --live missing or ignored (the operational refit would have no command), a
    --live run that writes anywhere but the root the paper runtime reads."""
    rc = main(["--live"])

    assert rc == 0
    model = json.loads((live_root / MODEL).read_text(encoding="utf-8"))
    assert sorted(model["leads"]) == ["1", "2", "3"]  # the synthetic series carry leads 1-3
    assert (live_root / REPORT_MD).exists()
    report = json.loads((live_root / REPORT_JSON).read_text(encoding="utf-8"))
    assert report["artifact_paths"]["model"] == str(live_root / MODEL)


def test_fit_with_root_and_live_together_exits_2_and_fits_neither(live_root, tmp_path):
    """Kills: one of two contradictory roots winning silently (a stray --live sending a
    comparison refit to the live root, a stray --root sending the operational refit away)."""
    snapshot = _write_fit_inputs(tmp_path / "snapshot")

    status = _exit_status(["--root", str(snapshot), "--live"])

    assert status == 2
    assert (live_root / MODEL).read_text(encoding="utf-8") == OPERATIONAL
    assert not (live_root / "reports").exists()
    assert not (snapshot / "models").exists() and not (snapshot / "reports").exists()


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_fit_with_a_blank_root_exits_2_instead_of_falling_back_to_the_default_root(
    blank, live_root, capsys
):
    """Kills: --root "$UNSET" reaching climate.paths.data_root, which reads a blank override
    as no override and resolves the default root, so a comparison refit with --root on its
    command line would overwrite the operational model."""
    status = _exit_status(["--root", blank])
    captured = capsys.readouterr()

    assert status == 2
    assert str(live_root / MODEL) in captured.err  # the refusal still says what was at stake
    assert (live_root / MODEL).read_text(encoding="utf-8") == OPERATIONAL
    assert not (live_root / "reports").exists()


@pytest.mark.parametrize(
    "variable", [None, "~/elsewhere"], ids=["home-default", "tilde-in-the-variable"]
)
def test_refusal_names_the_root_data_root_resolves_and_creates_nothing(
    variable, tmp_path, monkeypatch, capsys
):
    """Kills: a refusal that writes (data_root() makes the root it returns, so calling it to
    word the refusal would create directories under the live home), and a refusal whose own
    idea of the default root has drifted from the one a bare fit would have used."""
    home = Path.home()
    assert home == tmp_path / "home"  # the redirect holds here: nothing below is a real home
    if variable is None:
        monkeypatch.delenv("CLIMATE_DATA_ROOT")
    else:
        monkeypatch.setenv("CLIMATE_DATA_ROOT", variable)

    status = _exit_status([])
    captured = capsys.readouterr()

    assert status == 2
    assert list(home.iterdir()) == []  # a refusal leaves nothing behind
    # Asked only now, because it creates what it returns. data_root() is the oracle on
    # purpose: a literal here would be a second copy of its defaults, free to drift with
    # the script's.
    assert str(data_root() / MODEL) in captured.err
