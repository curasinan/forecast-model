"""Watch the GTM simulations from a terminal.

With no option it lists the active runs and the latest finished ones.
`--follow [RUN]` keeps one run on screen, refreshed every second, until it
finishes, fails or dies, then prints its results; RUN is a run name, a
unique prefix of one, a run directory, or `latest` (the default: the newest
running run, else the newest). `--once` prints one snapshot and exits, for a
phone ssh client or a script. `--show RUN` prints a finished run's results.
`--clear-dead` removes the active entries of runs that died and of runs that
ended more than a day ago. It only reads progress files and run directories.

Exit codes: 0 running or finished, 1 failed or dead, 2 no such run, 130 the
watcher was interrupted (the run itself goes on).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.text import Text  # noqa: E402

from climate.sim.progress import (  # noqa: E402
    ACTIVE,
    PROGRESS_FILE,
    RunEntry,
    default_runs_root,
    find_run,
    list_runs,
    liveness,
    prune_active,
    read_progress,
)
from climate.sim.watch import render_list, render_results, render_run  # noqa: E402

LIVE_STATES = ("running", "stalled", "silent")


def _refresh(entry: RunEntry) -> RunEntry:
    """The run's current state: its active entry while it lasts, else its run directory's record."""
    if entry.active:
        state = read_progress(entry.path / PROGRESS_FILE)
        if state is not None:
            return RunEntry(entry.name, entry.path, state, True)
    run_dir = Path(entry.state.get("run_dir") or entry.path).expanduser()
    state = read_progress(run_dir / PROGRESS_FILE)
    if state is not None:
        return RunEntry(entry.name, run_dir, state, False)
    return entry


def _run_dir(entry: RunEntry) -> Path:
    return Path(entry.state.get("run_dir") or entry.path).expanduser()


def _exit_code(state) -> int:
    return 1 if liveness(state) in ("failed", "dead") else 0


def _final(console: Console, entry: RunEntry) -> int:
    console.print(render_run(entry.state, width=console.width))
    if entry.state.get("status") == "finished" and _run_dir(entry).is_dir():
        console.print()
        console.print(render_results(_run_dir(entry)))
    return _exit_code(entry.state)


def follow(console: Console, entry: RunEntry, interval: float) -> int:
    if liveness(entry.state) not in LIVE_STATES:
        return _final(console, entry)
    try:
        with Live(render_run(entry.state, width=console.width), console=console, refresh_per_second=4,
                  transient=True) as live:
            while liveness(entry.state) in LIVE_STATES:
                time.sleep(interval)
                entry = _refresh(entry)
                live.update(render_run(entry.state, width=console.width))
    except KeyboardInterrupt:
        console.print(Text("stopped watching; the run goes on", style="dim"))
        return 130
    return _final(console, entry)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-root", default=None,
                    help="where the run directories are (default ~/runs, as the simulations write)")
    ap.add_argument("--follow", nargs="?", const="latest", default=None, metavar="RUN",
                    help="keep RUN on screen until it ends (default: latest)")
    ap.add_argument("--once", action="store_true", help="print one snapshot of --follow's run and exit")
    ap.add_argument("--show", metavar="RUN", default=None, help="print a finished run's results")
    ap.add_argument("--clear-dead", action="store_true",
                    help="remove active entries of dead runs and of runs that ended a day ago")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between refreshes")
    ap.add_argument("--limit", type=int, default=10, help="finished runs to list (default 10)")
    args = ap.parse_args(argv)

    console = Console()
    root = Path(args.runs_root).expanduser() if args.runs_root else default_runs_root()

    if args.clear_dead:
        removed = prune_active(root, include_dead=True)
        console.print(f"removed {len(removed)} active entr{'y' if len(removed) == 1 else 'ies'}"
                      + (f": {', '.join(removed)}" if removed else "")
                      + f" under {root / ACTIVE}")
        return 0

    spec = args.show or args.follow or ("latest" if args.once else None)
    if spec is None:
        console.print(render_list(list_runs(root), limit=args.limit))
        return 0
    entry = find_run(root, spec)
    if entry is None:
        console.print(f"no run matches {spec!r} under {root}", style="red")
        return 2
    entry = _refresh(entry)
    if args.show:
        if not _run_dir(entry).is_dir():
            console.print(render_run(entry.state, width=console.width))
            console.print(Text("the run has not written its results yet", style="dim"))
            return _exit_code(entry.state)
        console.print(Group(render_run(entry.state, width=console.width), Text(""), render_results(_run_dir(entry))))
        return _exit_code(entry.state)
    if args.once:
        if liveness(entry.state) in LIVE_STATES:
            console.print(render_run(entry.state, width=console.width))
            return 0
        return _final(console, entry)
    return follow(console, entry, args.interval)


if __name__ == "__main__":
    sys.exit(main())
