"""Run directories of the simulations, written in one step."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd

CELL_FRAMES = ("decisions", "orders", "months", "equity")


def plain(value):
    """A JSON-ready stand-in for numpy numbers (NaN and infinity become null) and the rest."""
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    return str(value)


def write_run(run_dir: Path, *, config: dict, metrics: dict, grid: pd.DataFrame, report: str,
              cells: Mapping[str, tuple], extra: Mapping[str, Mapping[str, pd.DataFrame]] | None = None
              ) -> None:
    """config.json, metrics.json, grid.parquet, report.md and cells/<cell>/, then renamed into place.

    `cells` maps a cell's name to (its ReplayResult, the path of its paper
    book or None); each cell gets the result's four frames, the book as
    paper_book.db, and the frames `extra[name]` names. FileExistsError when
    the run directory is there already.
    """
    run_dir = Path(run_dir)
    if run_dir.exists():
        raise FileExistsError(f"the run directory exists already: {run_dir}")
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    scratch = run_dir.with_name(f".{run_dir.name}.{uuid.uuid4().hex[:8]}.partial")
    scratch.mkdir()
    try:
        (scratch / "config.json").write_text(json.dumps(config, indent=2, default=str),
                                             encoding="utf-8")
        (scratch / "metrics.json").write_text(json.dumps(metrics, indent=2, default=plain),
                                              encoding="utf-8")
        grid.to_parquet(scratch / "grid.parquet", index=False)
        (scratch / "report.md").write_text(report, encoding="utf-8", newline="\n")
        for name, (result, book) in cells.items():
            cell = scratch / "cells" / name
            cell.mkdir(parents=True)
            for frame in CELL_FRAMES:
                getattr(result, frame).to_parquet(cell / f"{frame}.parquet", index=False)
            if book is not None and Path(book).exists():
                shutil.copy2(book, cell / "paper_book.db")
            for extra_name, frame in (extra or {}).get(name, {}).items():
                frame.to_parquet(cell / f"{extra_name}.parquet", index=False)
        os.rename(scratch, run_dir)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def finish_run(writer, run_dir: Path, headline: Mapping | None = None) -> Path | None:
    """Write the run's dashboard.html, then mark the run finished; returns the dashboard's path.

    The results are already on disk by then, so a dashboard that cannot be
    rendered costs a warning in the progress log, never the run.
    """
    path = None
    try:
        from climate.sim.dashboard import write_dashboard

        path = write_dashboard(run_dir)
    except Exception as exc:  # noqa: BLE001 -- the run's results stand without it
        writer.warning(None, f"dashboard not written: {type(exc).__name__}: {exc}")
    writer.finished(headline)
    return path


__all__ = ["CELL_FRAMES", "finish_run", "plain", "write_run"]
