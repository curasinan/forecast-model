"""Write (or rewrite) the HTML dashboard of a GTM simulation run.

Every simulation script writes dashboard.html into its run directory at the
end. This script renders it again, for an older run or after a code change,
or renders a run still going from the cells it has finished (the page says
it is partial and refreshes itself). RUN is a run name, a unique prefix, a
run directory, or `latest`. The page is one self-contained file: copy it
anywhere and open it in a browser.

Exit codes: 0 written, 2 no such run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.sim.dashboard import write_dashboard  # noqa: E402
from climate.sim.progress import default_runs_root, find_run  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", nargs="?", default="latest", help="run name, prefix, directory or latest")
    ap.add_argument("--runs-root", default=None, help="where the run directories are (default ~/runs)")
    ap.add_argument("--out", default=None, help="where to write the page (default: in the run)")
    args = ap.parse_args(argv)
    root = Path(args.runs_root).expanduser() if args.runs_root else default_runs_root()
    entry = find_run(root, args.run)
    if entry is None:
        print(f"no run matches {args.run!r} under {root}", file=sys.stderr)
        return 2
    run_dir = Path(entry.state.get("run_dir") or entry.path).expanduser()
    source = run_dir if (run_dir / "config.json").exists() else entry.path
    try:
        path = write_dashboard(source, args.out)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
