"""Every entry point that writes live state refuses a root that carries the mirror marker."""

from __future__ import annotations

import importlib
import sys

import pytest

from src.utils.live_root import MIRROR_MARKER


class ReachedTheWork(AssertionError):
    """The entry point went past the point where it had to refuse."""


def _trap(*_args, **_kwargs):
    raise ReachedTheWork("the entry point did not refuse the marked root")


# (module, argv, attribute the refusal must come before)
CASES = [
    ("scripts.record_ibkr_ticks", ["--once"], "QuoteRecorder"),
    ("scripts.sync_ibkr_paper", [], "IBKRAdapter"),
    ("scripts.run_ibkr_paper", ["--simulate"], "data_root"),
    ("scripts.run_gtm_paper", [], "data_root"),
    ("scripts.backfill_ibkr_hourly", ["--dry-run", "--port", "4002", "--mode", "paper"],
     "ContractResolver"),
    ("scripts.collect_climate_labels", [], "data_root"),
    ("scripts.fetch_climate_forecasts", ["--pilot"], "climate_data_root"),
    ("scripts.fit_gtm_emos", ["--trend", "--live"], "fit_gtm"),
]
IDS = [case[0].split(".")[-1] for case in CASES]


def _wire(monkeypatch, tmp_path, default_root) -> None:
    """Make ``default_root`` both lanes' default root, with a paper session's settings."""
    monkeypatch.setenv("IBKR_DATA_ROOT", str(default_root / "ibkr_ticks"))
    monkeypatch.setenv("CLIMATE_DATA_ROOT", str(default_root / "climate_data"))
    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.setenv("IBKR_TRADING_MODE", "paper")
    monkeypatch.chdir(tmp_path)  # no repo .env, no logs/ in the repo


@pytest.fixture
def marked_root(tmp_path, monkeypatch):
    """A quant-data root that is a mirror, wired in as both lanes' default root."""
    quant = tmp_path / "quant_data"
    quant.mkdir()
    (quant / MIRROR_MARKER).write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")
    _wire(monkeypatch, tmp_path, quant)
    return quant


@pytest.fixture
def live_root_and_mirror(tmp_path, monkeypatch):
    """An unmarked root wired in as the default, and a marked one beside it."""
    live = tmp_path / "live" / "quant_data"
    live.mkdir(parents=True)
    mirror = tmp_path / "mirror" / "quant_data"
    mirror.mkdir(parents=True)
    (mirror / MIRROR_MARKER).write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")
    _wire(monkeypatch, tmp_path, live)
    return live, mirror


def _booby_trap(module_name: str, trapped: str, monkeypatch):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, trapped, _trap)
    monkeypatch.setattr(module, "setup_logging", lambda *a, **k: None, raising=False)
    return module


def _run(module_name: str, argv: list[str], monkeypatch) -> int:
    module = importlib.import_module(module_name)
    monkeypatch.setattr(sys, "argv", [module_name, *argv])
    try:
        # record_ibkr_ticks.main() takes no argv; the others take a list
        result = module.main() if module_name.endswith("record_ibkr_ticks") else module.main(argv)
    except SystemExit as exc:
        result = exc.code
    return int(result)


@pytest.mark.parametrize(("module_name", "argv", "trapped"), CASES, ids=IDS)
def test_the_entry_point_refuses_a_mirror_before_doing_anything(
        module_name, argv, trapped, marked_root, monkeypatch, capsys):
    _booby_trap(module_name, trapped, monkeypatch)

    rc = _run(module_name, argv, monkeypatch)
    captured = capsys.readouterr()

    assert rc == 2
    assert "mirror of the live root" in captured.out + captured.err
    assert sorted(p.name for p in marked_root.rglob("*")) == [MIRROR_MARKER]  # nothing was created


@pytest.mark.parametrize(("module_name", "argv", "trapped"), CASES, ids=IDS)
def test_the_same_entry_point_goes_to_work_on_a_root_without_the_marker(
        module_name, argv, trapped, live_root_and_mirror, monkeypatch):
    """The control: without the marker the trap is reached, so the refusal is not unconditional."""
    _booby_trap(module_name, trapped, monkeypatch)

    with pytest.raises(ReachedTheWork):
        _run(module_name, argv, monkeypatch)


def test_a_paper_book_named_by_hand_inside_a_mirror_is_refused_however_deep(
        live_root_and_mirror, monkeypatch, capsys):
    _live, mirror = live_root_and_mirror
    sync_cli = _booby_trap("scripts.sync_ibkr_paper", "IBKRAdapter", monkeypatch)
    book = mirror / "ibkr_ticks" / "paper" / "archive" / "ibkr_paper.db"

    assert sync_cli.main(["--db", str(book)]) == 2
    assert "mirror of the live root" in capsys.readouterr().err


def test_placing_orders_refuses_a_book_or_a_climate_root_inside_a_mirror(
        live_root_and_mirror, monkeypatch, capsys):
    _live, mirror = live_root_and_mirror
    paper_cli = _booby_trap("scripts.run_ibkr_paper", "data_root", monkeypatch)
    book = mirror / "ibkr_ticks" / "paper" / "ibkr_paper_sim.db"

    assert paper_cli.main(["--simulate", "--db", str(book)]) == 2
    monkeypatch.setenv("CLIMATE_DATA_ROOT", str(mirror / "climate_data"))
    assert paper_cli.main(["--simulate"]) == 2
    assert capsys.readouterr().err.count("mirror of the live root") == 2


def test_a_comparison_refit_on_a_named_mirror_is_still_allowed(marked_root, monkeypatch):
    """`--root <mirror>` is the documented research use; only `--live` is refused."""
    import scripts.fit_gtm_emos as fit_cli

    monkeypatch.setattr(fit_cli, "fit_gtm", _trap)
    with pytest.raises(ReachedTheWork):
        fit_cli.main(["--trend", "--root", str(marked_root / "climate_data")])
