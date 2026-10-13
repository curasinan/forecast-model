"""Run a command, then tell an external check that it ran."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request

ATTEMPTS = 3
TIMEOUT_S = 10.0
RETRY_PAUSE_S = 5.0


def _validate_env_name(value: str) -> str:
    """Validate that value is an environment variable name, not a URL or other string."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        raise argparse.ArgumentTypeError("--url-env takes an environment variable NAME")
    return value


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
    parser.add_argument("--url-env", required=True, type=_validate_env_name,
                        help="NAME of the environment variable that holds the ping URL")
    parser.add_argument("--ok-exit", nargs="+", type=int, default=[0],
                        help="exit statuses of the command that count as 'it ran'")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the command to run")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given; put it after --")

    try:
        status = run(command)
    except OSError as exc:
        exc_name = type(exc).__name__
        print(f"heartbeat: not sent (command could not be started: {exc_name})")
        return 127 if isinstance(exc, FileNotFoundError) else 126
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
