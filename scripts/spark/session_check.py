"""Is an IB Gateway session possible on this machine right now?"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

DEFAULT_PORT = 4002


class CheckFailed(RuntimeError):
    """The tool that answers the question could not be run."""


def _output(cmd: list[str], run) -> str:
    try:
        done = run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckFailed(f"could not run {cmd[0]}: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise CheckFailed(f"could not run {cmd[0]}: exit {done.returncode}")
    return done.stdout or ""


def listening_ports(run=subprocess.run) -> set[int]:
    """Local TCP ports with a listening socket."""
    if sys.platform == "win32":
        tool = "netstat"
        text = _output([tool, "-an"], run)
        lines = [line for line in text.splitlines() if "LISTENING" in line]
        pattern = re.compile(r"^\s*TCP\s+.+?:(\d+)\s")
    else:
        tool = "ss"
        text = _output([tool, "-ltnH"], run)
        lines = text.splitlines()
        pattern = re.compile(r"^\s*LISTEN\s+\d+\s+\d+\s+\S+:(\d+)\s")
    ports: set[int] = set()
    for line in lines:
        match = pattern.match(line)
        if match:
            ports.add(int(match.group(1)))
    if text.strip() and not ports:
        raise CheckFailed(f"no listening port could be read from the {tool} output "
                          f"({len(text.splitlines())} lines)")
    return ports


def process_running(name: str, run=subprocess.run) -> bool:
    """True when a process whose command line contains ``name`` runs (excluding this checker and its parent)."""
    if sys.platform == "win32":
        return name.lower() in _output(["tasklist"], run).lower()
    try:
        done = run(["pgrep", "-f", name], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckFailed(f"could not run pgrep: {type(exc).__name__}") from exc
    if done.returncode not in (0, 1):  # 1 is pgrep's "no process matched"
        raise CheckFailed(f"could not run pgrep: exit {done.returncode}")
    if done.returncode == 1:  # No process matched
        return False
    # Filter out this checker's pid and its parent
    own_pid = os.getpid()
    parent_pid = os.getppid()
    pids = [int(pid) for pid in done.stdout.strip().split('\n') if pid.strip()]
    remaining_pids = [pid for pid in pids if pid not in (own_pid, parent_pid)]
    return bool(remaining_pids)


def container_running(name: str, run=subprocess.run) -> bool:
    """True when a Docker container with exactly this name is up."""
    cmd = ["docker", "ps", "--quiet", "--filter", f"name=^{name}$"]
    return bool(_output(cmd, run).strip())


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--process", default=None, help="a process name to look for")
    parser.add_argument("--container", default=None, help="a Docker container name to look for")
    args = parser.parse_args(argv)

    found: list[str] = []
    try:
        if args.port in listening_ports(run):
            found.append(f"something listens on port {args.port}")
        if args.process and process_running(args.process, run):
            found.append(f"a process named {args.process} runs")
        if args.container and container_running(args.container, run):
            found.append(f"the container {args.container} runs")
    except CheckFailed as exc:
        print(f"session check: {exc}; treating the session as possibly here")
        return 1
    if found:
        print("session check: a Gateway is here: " + "; ".join(found))
        return 1
    print(f"session check: no Gateway here (port {args.port} closed"
          + (f", no {args.process} process" if args.process else "")
          + (f", container {args.container} not running" if args.container else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
