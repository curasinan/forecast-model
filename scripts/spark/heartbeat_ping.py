"""Run a command, then tell an external check that it ran.

    python scripts/spark/heartbeat_ping.py --url-env HEARTBEAT_URL_HEALTH --ok-exit 0 1 -- \\
        python scripts/check_ibkr_recorder.py

The command's exit status is this script's exit status, always: a ping that
fails never turns a good run into a bad one, nor the reverse. When the status
is one of --ok-exit (default: 0), the URL held by the named environment
variable is fetched (GET, 10 s timeout, three attempts).

The URL is a capability: whoever holds it can fake the signal. It is read
from the environment, never from an argument, and it is NEVER printed -- not
in a message and not through an exception, whose text usually quotes it. Only
"sent", "not sent" and the class name of an error leave this script.
(docs/plans/2026-10-05-spark-runtime-move-design.md, section 8.)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

ATTEMPTS = 3
TIMEOUT_S = 10.0
RETRY_PAUSE_S = 5.0


def ping(url: str, opener=urllib.request.urlopen, pause=time.sleep) -> str | None:
    """None when the URL answered, else the class name of the last error."""
    error = "NoAttempt"
    for attempt in range(ATTEMPTS):
        try:
            opener(url, timeout=TIMEOUT_S)
            return None
        except Exception as exc:  # noqa: BLE001 -- only its class name may be shown
            error = type(exc).__name__
            if attempt + 1 < ATTEMPTS:
                pause(RETRY_PAUSE_S)
    return error


def main(argv: list[str] | None = None, run=subprocess.call,
         opener=urllib.request.urlopen, pause=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url-env", required=True,
                        help="NAME of the environment variable that holds the ping URL")
    parser.add_argument("--ok-exit", nargs="+", type=int, default=[0],
                        help="exit statuses of the command that count as 'it ran'")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the command to run")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given; put it after --")

    status = run(command)
    if status not in args.ok_exit:
        print(f"heartbeat: not sent (command exit {status})")
        return status
    url = os.environ.get(args.url_env, "").strip()
    if not url:
        print(f"heartbeat: not sent ({args.url_env} is not set)")
        return status
    if not url.startswith("https://"):
        print(f"heartbeat: not sent ({args.url_env} is not an https URL)")
        return status
    error = ping(url, opener=opener, pause=pause)
    print("heartbeat: sent" if error is None else f"heartbeat: failed ({error})")
    return status


if __name__ == "__main__":
    sys.exit(main())
